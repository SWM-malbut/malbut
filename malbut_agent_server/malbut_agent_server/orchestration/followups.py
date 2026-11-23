"""Read-only tool follow-up synthesis and standalone courtesy replies."""

import copy
import re
import uuid
from concurrent.futures import CancelledError
from dataclasses import replace
from typing import Any, Callable, Dict, Sequence

from malbut_agent_server.conversation import (
    BeginTurnToken,
    ConversationSummary,
    ConversationTurn,
    SQLiteConversationStore,
)
from malbut_agent_server.conversation_preferences import CHOICES, DEFAULTS
from malbut_agent_server.conversation_progress import (
    SERVICE_UNAVAILABLE_NOTICE,
    WEATHER_RETRY_NOTICE,
    announce_function_start,
    claim_retry,
    delayed_function_start,
)
from malbut_agent_server.homecam_query import record_existence_reply
from malbut_agent_server.prompting import bounded_weather_context
from malbut_agent_server.providers.base import (
    AgentProvider,
    accepts_memory_context,
    accepts_weather_context,
)
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
    ProviderResult,
    ProviderUsage,
    SpeechAgentRequest,
    copy_request,
)
from malbut_agent_server.weather import (
    current_temperature_reply,
    daily_forecast_reply,
)


def thanks_after_answer(
    conversation_store: SQLiteConversationStore,
    request: AgentRequest,
    snapshot: Any,
    token: BeginTurnToken,
) -> ProviderResult | None:
    """A bare thank-you after a settled voice answer keeps the session open."""
    # Later turns may carry free-form instructions outside the saved response settings.
    if (not isinstance(request, SpeechAgentRequest) or token.ordinal != 2
            or not re.fullmatch(
                r'\s*(?:고마워요?|고맙습니다|감사해요?|감사합니다)[.!。！]?\s*',
                request.utterance,
            )
            or request.navigation_confirmation or request.mapping_name_requested
            or snapshot.pending or not snapshot.history):
        return None
    previous = snapshot.history[-1]
    receipt = previous.response.get('public', {})
    decision = receipt.get('decision', {})
    settings = snapshot.context.get('response_settings', {})
    tone = settings.get('tone')
    # STT may omit punctuation. A complete Korean count question still has
    # an explicit interrogative, without interpreting an arbitrary clause.
    question = re.fullmatch(
        r'[^.!?。！？,，\n]+[?？]|(?:[가-힣]+(?:에는|은|는)\s+)?[가-힣]+[이가은는]\s*몇\s*개\s*(?:있어(?:요)?|야|예요|인가요)',
        previous.user_content.strip(),
    )
    if (previous.ordinal != 1 or not question
            or re.search(r'(?:앞으로|하고|말고)\s', previous.user_content)
            or not re.search(r'[가-힣]', previous.assistant_content or '')
            or decision.get('type') != 'message'
            or (decision.get('reason') or '').replace('_', ' ') not in {
                'general knowledge', '일반 지식 답변',
            }
            or decision.get('message') != previous.assistant_content
            or receipt.get('safety', {}).get('code') != 'not_an_action'
            or 'weather_tool_decision' in previous.response
            or re.search(
                r'[?？]|(?:주세요|할까요|실래요|해줄래|알려줘)[.!。！]?$',
                previous.assistant_content,
            )
            or tone not in CHOICES['tone']
            or settings != dict(DEFAULTS, tone=tone, response_mode='natural')
            or conversation_store.pending_confirmation(
                request.user_id, request.conversation_id,
            )):
        return None
    text = '천만에요.' if tone == '편안한 존댓말' else '도움이 됐다니 기뻐.'
    return ProviderResult(
        AgentDecision('message', text, reason='courtesy_response'),
        'malbut-dialogue', 'standalone-thanks', 0, memory_supported=True,
    )


# Prerecorded homecam/location start notices take ~3.0-3.5s; only emit one if
# the read/update genuinely stalls past this threshold.
HOMECAM_START_NOTICE_DELAY_S = 1.5
WEATHER_LOCATION_START_NOTICE_DELAY_S = 1.5


def answer_homecam(
    provider: AgentProvider,
    homecam_executor: Callable[[Any, Dict[str, Any]], Dict[str, Any]] | None,
    request: AgentRequest,
    memories: Sequence[Any],
    turns: Sequence[ConversationTurn],
    summary: ConversationSummary | None,
    first_result: ProviderResult,
    *,
    memory_context: Dict[str, Any] | None,
) -> ProviderResult:
    """Read cloud metadata directly, then answer with no executable Tools."""
    with delayed_function_start(
        first_result.decision.tool_name,
        delay_s=HOMECAM_START_NOTICE_DELAY_S,
    ):
        try:
            if homecam_executor is None or not accepts_memory_context(provider):
                raise RuntimeError('homecam query unavailable')
            result = homecam_executor(
                first_result.decision.tool_name,
                first_result.decision.arguments,
            )
        except CancelledError:
            raise
        except Exception:
            result = {'success': False, 'code': 'UNAVAILABLE', 'result': {}}
        if not result['success']:
            message = (
                '웹에서 소유자의 음성 홈캠 조회 허용이 필요해요.'
                if result['code'] == 'VOICE_DELEGATION_REQUIRED'
                else '홈캠 정보를 조회하지 못했어요. 잠시 후 다시 요청해 주세요.'
            )
            return replace(first_result, decision=AgentDecision(
                'message', message, reason='homecam_unavailable',
            ), memory_proposal=None)
        confirmed = (
            result['success'] is True and result.get('code') == 'OK'
            and isinstance(result['result'], dict)
        )
        settings = (memory_context or {}).get('response_settings')
        if (confirmed and isinstance(request, SpeechAgentRequest)
                and settings == dict(DEFAULTS, response_mode='natural')):
            reply = record_existence_reply(
                request.utterance, first_result.decision.tool_name, result['result'],
            )
            if reply is not None:
                return replace(first_result, decision=AgentDecision(
                    'message', reply, reason='homecam_record_existence',
                ), memory_proposal=None)
        context: Dict[str, Any] = copy.deepcopy(memory_context) if memory_context is not None else {}
        # This is current-query data, not a remembered fact or workflow journal.
        context['homecam_query_result_untrusted'] = {
            'tool': first_result.decision.tool_name, 'data': result['result'],
        }
        value = request.to_dict()
        value['available_tools'] = []
        followup = copy_request(request, value)
        if isinstance(followup, SpeechAgentRequest):
            followup = replace(followup, confirmed_homecam_read=confirmed)
        answer = provider.complete(
            followup, list(memories), copy.deepcopy(list(turns)), [],
            conversation_summary=copy.deepcopy(summary), memory_context=context,
        )
    answer.validate()
    if answer.decision.type == 'tool_call' or answer.memory_proposal is not None:
        return replace(first_result, decision=AgentDecision(
            'refusal', '홈캠 조회 결과로 답변을 만들지 못했어요.',
            reason='homecam_followup_tool_forbidden',
        ), memory_proposal=None)
    return replace(
        answer, latency_ms=first_result.latency_ms + answer.latency_ms,
        usage=ProviderUsage(**{
            name: (
                getattr(first_result.usage, name) + getattr(answer.usage, name)
                if getattr(first_result.usage, name) is not None
                and getattr(answer.usage, name) is not None else None
            )
            for name in ('input_tokens', 'output_tokens', 'total_tokens')
        }),
    )


def answer_weather(
    provider: AgentProvider,
    weather_executor: Callable[[str], Dict[str, Any]] | None,
    weather_location_executor: Callable[[str, str], Dict[str, Any]] | None,
    request: AgentRequest,
    memories: Sequence[Any],
    conversation_turns: Sequence[ConversationTurn],
    conversation_summary: ConversationSummary | None,
    first_result: ProviderResult,
    *,
    memory_context: Dict[str, Any] | None = None,
    weather_context_checker: Callable[[Any], bool] = accepts_weather_context,
) -> ProviderResult:
    """Execute one Manager read after the model's Tool choice, then answer without Tools."""
    from malbut_agent_server.mission_audio import CATALOG

    setting_location = first_result.decision.tool_name == 'set_weather_location'
    has_executor = (
        weather_location_executor is not None
        if setting_location
        else weather_executor is not None
    )
    if not has_executor or not weather_context_checker(provider):
        return replace(first_result, decision=AgentDecision(
            type='message',
            message=(
                SERVICE_UNAVAILABLE_NOTICE if setting_location
                else CATALOG['weather.fetch_failed']
            ),
            reason='weather_unavailable', confidence=1.0,
        ), memory_proposal=None)
    weather: Dict[str, Any] = {'status': 'unavailable'}
    if setting_location:
        with delayed_function_start(
            first_result.decision.tool_name,
            delay_s=WEATHER_LOCATION_START_NOTICE_DELAY_S,
        ):
            try:
                assert weather_location_executor is not None
                weather = weather_location_executor(
                    request.request_id,
                    first_result.decision.arguments['location'],
                )
                weather = bounded_weather_context(weather)
            except CancelledError:
                raise
            except Exception:
                weather = {'status': 'unavailable'}
    else:
        announce_function_start(first_result.decision.tool_name)
        for attempt in range(2):
            try:
                assert weather_executor is not None
                # A new read ID avoids replaying the Manager's failed receipt.
                read_id = request.request_id if attempt == 0 else str(uuid.uuid4())
                weather = weather_executor(read_id)
                weather = bounded_weather_context(weather)
            except CancelledError:
                raise
            except Exception:
                weather = {'status': 'unavailable'}
            if (weather['status'] != 'unavailable' or attempt
                    or not claim_retry(WEATHER_RETRY_NOTICE)):
                break
    notice_id = weather.get('notice_id')
    if not setting_location:
        if weather['status'] == 'location_required':
            notice_id = 'weather.location_required'
        elif weather['status'] == 'unavailable' and notice_id != 'weather.location_required':
            notice_id = 'weather.fetch_failed'
    if (weather['status'] in {'unavailable', 'location_required'}
            and isinstance(notice_id, str) and notice_id in {
                'operation.failed', 'operation.unavailable', 'operation.unknown',
                'weather.location_required', 'weather.fetch_failed',
            }):
        return replace(first_result, decision=AgentDecision(
            type='message', message=CATALOG[notice_id], reason=notice_id,
            confidence=1.0,
        ), memory_proposal=None)
    if setting_location:
        # A committed setting needs a receipt even if a second model call would fail.
        status = weather['status']
        location = weather.get('location')
        if status == 'location_set' and isinstance(location, str) and location.strip():
            decision = AgentDecision(
                'message', CATALOG['set_weather_location.succeeded'],
                reason='weather_location_saved', confidence=1.0,
            )
        elif status == 'location_ambiguous':
            decision = AgentDecision(
                'clarification',
                '어느 지역인가요? ' + ', '.join(weather['candidates'])
                + ' 중에서 시·구·동을 알려주세요.',
                reason='weather_location_ambiguous',
            )
        elif status == 'location_not_found':
            decision = AgentDecision(
                'clarification', '지역을 찾지 못했어요. 시·구·동을 더 자세히 알려주세요.',
                reason='weather_location_not_found',
            )
        else:
            decision = AgentDecision(
                'message', '날씨 조회 위치를 저장하지 못했어요. 잠시 후 다시 알려주세요.',
                reason='weather_location_unavailable',
            )
        return replace(first_result, decision=decision, memory_proposal=None)
    settings = (memory_context or {}).get('response_settings', {})
    for render, reason in (
        (daily_forecast_reply, 'weather_daily_forecast'),
        (current_temperature_reply, 'weather_current_temperature'),
    ):
        reply = render(request.utterance, weather, settings)
        if reply is not None:
            return replace(first_result, decision=AgentDecision(
                'message', reply, reason=reason, confidence=1.0,
            ), memory_proposal=None)
    value = request.to_dict()
    value['available_tools'] = []
    extra_kwargs: Dict[str, Any] = (
        {'memory_context': copy.deepcopy(memory_context)}
        if memory_context is not None and accepts_memory_context(provider)
        else {}
    )
    answer = provider.complete(
        copy_request(request, value), list(memories),
        copy.deepcopy(list(conversation_turns)), [],
        conversation_summary=copy.deepcopy(conversation_summary),
        weather_context=weather,
        **extra_kwargs,
    )
    answer.validate()
    if (weather['status'] == 'fresh'
            and answer.provider == 'reliable-fallback'
            and answer.model == 'safe-non-action'
            and answer.decision.reason == 'provider_unavailable'):
        answer = replace(answer, decision=AgentDecision(
            type='message',
            message=SERVICE_UNAVAILABLE_NOTICE,
            reason='weather_answer_unavailable', confidence=1.0,
        ), memory_proposal=None)
    if answer.decision.type == 'tool_call' or answer.memory_proposal is not None:
        answer = replace(answer, decision=AgentDecision(
            type='refusal', message='날씨 조회 결과로 답변을 만들지 못했어요.',
            reason='weather_followup_tool_forbidden', confidence=1.0,
        ), memory_proposal=None)

    def combined(name: str) -> int | None:
        first = getattr(first_result.usage, name)
        second = getattr(answer.usage, name)
        return first + second if first is not None and second is not None else None

    return replace(
        answer, latency_ms=first_result.latency_ms + answer.latency_ms,
        usage=ProviderUsage(**{name: combined(name) for name in (
            'input_tokens', 'output_tokens', 'total_tokens',
        )}),
        input_chars=(
            first_result.input_chars + answer.input_chars
            if first_result.input_chars is not None and answer.input_chars is not None
            else None
        ),
    )
