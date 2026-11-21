"""Tests for offline safe degradation when internet or OpenAI is unavailable."""

import json
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest

from malbut_agent_server.config import Settings
from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.conversation_progress import SERVICE_UNAVAILABLE_NOTICE
from malbut_agent_server.fall_check_watch import FALL_CHECK_RESPONSE
from malbut_agent_server.http_server import make_server
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.offline_fallback import (
    OFFLINE_MODEL_NAME,
    OFFLINE_PROVIDER_NAME,
    current_timeout_cap,
    is_urgent_stop_utterance,
    resolve_offline_speech_fallback,
)
from malbut_agent_server.orchestrator import AgentOrchestrator
from malbut_agent_server.providers.base import AgentProvider
from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
from malbut_agent_server.providers.reliable import (
    NormalizedProviderError,
    ProviderFailureCode,
    ReliableProvider,
)
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
    ProviderResult,
    RobotState,
    SpeechAgentRequest,
)
from malbut_agent_server.speech_dialogue import DialogueWorker
from malbut_agent_server.speech_mission_policy import configure_speech_missions
from malbut_agent_server.speech_missions import SpeechMissions
from malbut_agent_server.speech_navigation import NavigationTarget


class _FailingProvider(AgentProvider):
    """Simulate OpenAI network/timeout/key outage while recording call details."""

    name = 'openai-outage'
    model = 'gpt-5-test'

    def __init__(self, error=None, *, healthy_reply=None):
        self.error = error or TimeoutError('simulated network blackhole')
        self.healthy_reply = healthy_reply
        self.calls = 0
        self.seen_timeout_caps = []

    def complete(
        self,
        request,
        memories,
        conversation_turns,
        tools,
        conversation_summary=None,
    ):
        del request, memories, conversation_turns, tools, conversation_summary
        self.calls += 1
        self.seen_timeout_caps.append(current_timeout_cap())
        if self.healthy_reply is not None:
            return ProviderResult(
                decision=self.healthy_reply,
                provider='openai',
                model=self.model,
                latency_ms=12.0,
            )
        raise self.error


class _FakeManager:
    """In-memory Manager client verifying local offline mission dispatch."""

    def __init__(self):
        self.submitted = []
        self.canceled = []
        self.records = {}

    def submit(self, capability_id, arguments, request_id):
        self.submitted.append((capability_id, arguments, request_id))
        self.records[request_id] = {
            'request_id': request_id,
            'capability_id': capability_id,
            'kind': 'accepted',
            'state': 'RUNNING',
            'accepted': True,
            'terminal': False,
        }
        return request_id

    def snapshot(self, request_id):
        return dict(self.records[request_id])

    def cancel(self, request_id):
        self.canceled.append(request_id)
        return {**self.records[request_id], 'kind': 'cancel_requested'}


class _FakeNavigationTargets:
    """Deterministic active map catalog with registered indoor destinations."""

    def __init__(self, names=('거실', '주방', '충전소')):
        self._names = tuple(names)

    def names(self, active_map):
        return self._names if active_map else ()

    def resolve(self, location, active_map):
        if not active_map or location not in self._names:
            raise ValueError('target_not_found')
        return NavigationTarget(
            location=location,
            map_path=active_map,
            frame_id='map',
            x=1.0,
            y=2.0,
            yaw=0.0,
            digest='a' * 64,
        )

    def scene(self, active_map):
        del active_map
        return None


def _speech_request(
    utterance,
    *,
    available_tools=('cancel_voice_mission', 'request_navigation', 'request_recovery'),
    navigation_locations=('거실', '주방', '충전소'),
    navigation_confirmation='',
    mapping_name_requested=False,
    request_id='req-1',
    conversation_id='conv-1',
):
    return SpeechAgentRequest(
        request_id=request_id,
        user_id='speaker',
        conversation_id=conversation_id,
        turn_id=f'turn-{request_id}',
        utterance=utterance,
        robot_state=RobotState(),
        available_tools=tuple(available_tools),
        navigation_locations=navigation_locations,
        navigation_confirmation=navigation_confirmation,
        mapping_name_requested=mapping_name_requested,
    )


def _wait_reply(worker, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        replies = [r for r in worker.drain(validate=False) if r['kind'] not in {'progress', 'acknowledgement'}]
        if replies:
            return replies[0]
        time.sleep(0.005)
    raise AssertionError('dialogue worker did not produce a reply')


@pytest.mark.parametrize('utterance', [
    '멈춰', '멈춰줘', '멈춰요', '멈춰 주세요.', '정지', '정지해줘', '비상정지',
    '긴급정지', '스톱', 'stop', '그만', '그만해', '그만해줘', '취소', '취소해',
    '취소해줘', '작업 취소해', '이동 멈춰', '순찰 취소해줘', '따라오지 마',
    '이제 그만 따라와', '제이크 멈춰!', '말벗아 지금 바로 정지해줘',
])
def test_urgent_stop_matches_unambiguous_standalone_commands(utterance):
    assert is_urgent_stop_utterance(utterance)
    res = resolve_offline_speech_fallback(_speech_request(utterance), ('cancel_voice_mission',))
    assert res is not None
    assert res.decision.type == 'tool_call'
    assert res.decision.tool_name == 'cancel_voice_mission'
    assert res.provider == OFFLINE_PROVIDER_NAME
    assert res.model == OFFLINE_MODEL_NAME


@pytest.mark.parametrize('utterance', [
    '멈춰도 돼?',
    '취소할 수 있어?',
    '"멈춰"라고 말했어',
    '아까 멈춰라고 했잖아',
    '멈추지 마',
    '취소하지 마',
    '순찰을 취소하지 마',
    '멈추고 거실로 가',
    '순찰은 하지 말고 거실로 가',
    '날씨 조회 취소해',
    '기억 취소해',
    '상황 대응 취소해',
    '만약 멈춰라고 하면 어떻게 돼',
    '오늘 저녁 뭐 먹을까',
])
def test_urgent_stop_rejects_quotes_questions_negations_and_compound_clauses(utterance):
    assert not is_urgent_stop_utterance(utterance)
    res = resolve_offline_speech_fallback(_speech_request(utterance), ('cancel_voice_mission',))
    assert res is None


def test_pending_navigation_confirmation_blocks_cancel_and_accepts_affirmative():
    """Clarification cancellation must not cancel Manager missions; '응' confirms target."""
    req_cancel = _speech_request('취소해', navigation_confirmation='충전소')
    assert resolve_offline_speech_fallback(req_cancel, ('cancel_voice_mission', 'request_navigation')) is None

    req_yes = _speech_request('응', navigation_confirmation='충전소')
    res = resolve_offline_speech_fallback(req_yes, ('cancel_voice_mission', 'request_navigation'))
    assert res is not None
    assert res.decision.tool_name == 'request_navigation'
    assert res.decision.arguments == {'location': '충전소'}


@pytest.mark.parametrize('utterance,expected_location', [
    ('충전소로 가', '충전소'),
    ('충전소 가줘', '충전소'),
    ('충전하러 가줘', '충전소'),
    ('도크로 돌아가', '충전소'),
    ('거실로 가줘', '거실'),
    ('주방으로 이동해 주세요', '주방'),
    ('부엌으로 가', '주방'),
    ('go to dock please', '충전소'),
])
def test_offline_navigation_resolves_only_registered_active_map_locations(utterance, expected_location):
    res = resolve_offline_speech_fallback(_speech_request(utterance), ('request_navigation',))
    assert res is not None
    assert res.decision.tool_name == 'request_navigation'
    assert res.decision.arguments == {'location': expected_location}


@pytest.mark.parametrize('utterance', [
    '옥상으로 가줘',
    '거실로 가지 마',
    '충전소 안 갈래',
    '거실로 갈 수 있어?',
    '거실로 가고 주방으로 가',
    '"거실로 가"라고 말해봐',
])
def test_offline_navigation_rejects_unregistered_negated_or_meta_requests(utterance):
    res = resolve_offline_speech_fallback(_speech_request(utterance), ('request_navigation',))
    assert res is None


def test_offline_recovery_and_non_speech_request_isolation():
    """Recovery resolves on SpeechAgentRequest, while HTTP AgentRequest never triggers."""
    speech_res = resolve_offline_speech_fallback(_speech_request('복구해줘'), ('request_recovery',))
    assert speech_res is not None
    assert speech_res.decision.tool_name == 'request_recovery'

    http_req = AgentRequest(
        request_id='http-stop',
        user_id='http-user',
        conversation_id='http-conv',
        turn_id='t-1',
        utterance='멈춰',
        robot_state=RobotState(),
        available_tools=('cancel_voice_mission',),
    )
    assert resolve_offline_speech_fallback(http_req, ('cancel_voice_mission',)) is None


def test_urgent_stop_caps_timeout_suppresses_retries_and_preserves_healthy_llm():
    """Urgent stop fast-fails on outage (1 attempt, capped timeout), yet uses healthy LLM."""
    memory_store = SQLiteMemoryStore(':memory:')
    conversation_store = SQLiteConversationStore(':memory:')
    primary = _FailingProvider(error=NormalizedProviderError(ProviderFailureCode.NETWORK))
    fallback = _FailingProvider(error=NormalizedProviderError(ProviderFailureCode.NETWORK))
    reliable = ReliableProvider(
        [primary, fallback],
        max_retries=2,
        base_delay_seconds=0.5,
        max_delay_seconds=1.0,
        failure_threshold=2,
        attempt_timeout_seconds=10.0,
        total_timeout_seconds=30.0,
        sleep=lambda _s: None,
    )
    orchestrator = AgentOrchestrator(
        provider=reliable,
        memory_store=memory_store,
        conversation_store=conversation_store,
        safety_policy=SafetyPolicy(),
        offline_safety_fallback_enabled=True,
        offline_stop_timeout_seconds=2.5,
    )
    configure_speech_missions(orchestrator)
    try:
        session = conversation_store.create('speaker')
        res = orchestrator.handle(
            _speech_request('멈춰!', request_id='stop-1', conversation_id=session.conversation_id)
        )
        assert res.raw_decision.type == 'tool_call'
        assert res.raw_decision.tool_name == 'cancel_voice_mission'
        assert res.safety.allowed is True
        assert res.safety.code == 'manager_request'
        assert res.provider_result.provider == OFFLINE_PROVIDER_NAME
        # Fast-fail: 1 attempt on primary, 0 retries, 0 fallback cascade, 2.5s cap.
        assert primary.calls == 1
        assert fallback.calls == 0
        assert primary.seen_timeout_caps == [2.5]

        # Turn metrics record offline_fallback=True.
        snap = orchestrator.turn_metrics.snapshot(limit=5)
        assert snap['turns'][0]['flags'].get('offline_fallback') is True

        # Ordinary chat during outage still returns SERVICE_UNAVAILABLE_NOTICE.
        chat = orchestrator.handle(
            _speech_request('오늘 저녁 뭐 먹을까', request_id='chat-2', conversation_id=session.conversation_id)
        )
        assert chat.decision.type == 'refusal'
        assert chat.decision.message == SERVICE_UNAVAILABLE_NOTICE
        assert chat.provider_result.provider == 'reliable-fallback'
        # Primary circuit is now open; fallback needs one more failure to reach threshold=2.
        health_partial = orchestrator.provider_health()
        assert health_partial['circuits'][0]['state'] == 'open'
        assert health_partial['degraded'] is False

        # One turn fails fallback once (since primary is already open), second turn opens it.
        for idx in (3, 4):
            orchestrator.handle(
                _speech_request(
                    f'내일 일정 알려줘 {idx}',
                    request_id=f'chat-{idx}',
                    conversation_id=session.conversation_id,
                )
            )
        assert orchestrator.provider_health()['degraded'] is True

        # Healthy LLM still handles '멈춰' when reachable.
        healthy = _FailingProvider(
            healthy_reply=AgentDecision(
                type='tool_call',
                message='멈출게요.',
                tool_name='cancel_voice_mission',
                arguments={},
                reason='llm_stop',
                confidence=1.0,
            ),
        )
        orchestrator.provider = healthy
        ok_res = orchestrator.handle(
            _speech_request('멈춰', request_id='stop-online', conversation_id=session.conversation_id)
        )
        assert healthy.calls == 1
        assert ok_res.provider_result.provider == 'openai'
    finally:
        conversation_store.close()
        memory_store.close()


def test_openai_adapter_honors_offline_stop_timeout_cap():
    """OpenAIResponsesProvider passes the capped timeout to its transport."""
    seen_timeouts = []

    def fake_transport(url, headers, payload, timeout):
        del url, headers, payload
        seen_timeouts.append(timeout)
        raise TimeoutError('blackhole')

    adapter = OpenAIResponsesProvider(
        api_key='sk-test-key',
        model='gpt-5-mini',
        timeout_seconds=12,
        transport=fake_transport,
    )
    reliable = ReliableProvider(
        [adapter],
        max_retries=1,
        attempt_timeout_seconds=12.0,
        total_timeout_seconds=30.0,
        sleep=lambda _s: None,
    )
    memory_store = SQLiteMemoryStore(':memory:')
    conversation_store = SQLiteConversationStore(':memory:')
    orchestrator = AgentOrchestrator(
        provider=reliable,
        memory_store=memory_store,
        conversation_store=conversation_store,
        safety_policy=SafetyPolicy(),
        offline_stop_timeout_seconds=2.0,
    )
    configure_speech_missions(orchestrator)
    try:
        session = conversation_store.create('speaker')
        res = orchestrator.handle(
            _speech_request('정지해줘', request_id='cap-1', conversation_id=session.conversation_id)
        )
        assert seen_timeouts == [2.0]
        assert res.raw_decision.tool_name == 'cancel_voice_mission'
    finally:
        conversation_store.close()
        memory_store.close()


def test_end_to_end_offline_speech_worker_stop_navigation_confirm_and_fall_lock(tmp_path):
    """DialogueWorker executes cancel and registered navigation offline while honoring guards."""
    db_path = str(tmp_path / 'offline_dialogue.sqlite3')
    manager = _FakeManager()
    fall_locked = [False]
    missions = SpeechMissions(
        manager,
        navigation_targets=_FakeNavigationTargets(('거실', '주방', '충전소')),
        move_refusal=lambda: FALL_CHECK_RESPONSE if fall_locked[0] else None,
    )
    missions.observe_localization({'mode': 'LOCALIZATION', 'map': '/tmp/home.yaml'})

    outage_provider = ReliableProvider(
        [_FailingProvider(error=NormalizedProviderError(ProviderFailureCode.NETWORK))],
        max_retries=1,
        failure_threshold=1,
        sleep=lambda _s: None,
    )

    def factory():
        runtime = AgentOrchestrator(
            provider=outage_provider,
            memory_store=SQLiteMemoryStore(db_path),
            conversation_store=SQLiteConversationStore(db_path),
            safety_policy=SafetyPolicy(),
        )
        configure_speech_missions(runtime)
        return runtime

    worker = DialogueWorker(factory, 'speaker', missions=missions, publish_before_save=False)
    try:
        # 1) Direct exact registered navigation ('충전소로 가줘') works offline.
        assert worker.submit('u1', '충전소로 가줘')
        reply1 = _wait_reply(worker)
        spoken = []
        published1 = worker.publish_reply(reply1, lambda text: (spoken.append(text), True)[1])
        assert published1 is not None
        assert len(manager.submitted) == 1
        assert manager.submitted[0][0] == 'navigate_to_pose'

        # 2) Urgent stop ('멈춰!') cancels that running mission offline.
        assert worker.submit('u2', '멈춰!')
        reply2 = _wait_reply(worker)
        published2 = worker.publish_reply(reply2, lambda text: (spoken.append(text), True)[1])
        assert published2 is not None
        assert manager.canceled == [manager.submitted[0][2]]

        # 3) Alias ('충전하러 가') still triggers SpeechMissionPolicy confirmation offline,
        #    and '응' completes the navigation offline!
        assert worker.submit('u3', '충전하러 가')
        reply3 = _wait_reply(worker)
        assert '충전소' in reply3['text']
        assert worker.publish_reply(reply3, lambda text: (spoken.append(text), True)[1]) is not None
        assert len(manager.submitted) == 1  # Not dispatched yet!

        assert worker.submit('u4', '응')
        reply4 = _wait_reply(worker)
        assert worker.publish_reply(reply4, lambda text: (spoken.append(text), True)[1]) is not None
        assert len(manager.submitted) == 2

        # 4) Fall check lock STILL blocks offline navigation (zero safety bypass!).
        #    When fall check turns active between prepare and dispatch, SpeechMissions
        #    refuses with FALL_CHECK_RESPONSE; when already active at submit, DialogueWorker
        #    blocks before Manager dispatch. Either way, Manager never receives a Goal.
        fall_locked[0] = True
        assert worker.submit('u5', '거실로 가줘')
        reply5 = _wait_reply(worker)
        spoken_fall = []
        assert worker.publish_reply(reply5, lambda text: (spoken_fall.append(text), True)[1]) is not None
        assert len(manager.submitted) == 2

        fall_locked[0] = False
        assert worker.submit('u6', '거실로 가줘')
        reply6 = _wait_reply(worker)
        fall_locked[0] = True
        spoken_fall_dispatch = []
        assert worker.publish_reply(
            reply6, lambda text: (spoken_fall_dispatch.append(text), True)[1]
        ) is not None
        assert spoken_fall_dispatch == [FALL_CHECK_RESPONSE]
        assert len(manager.submitted) == 2
    finally:
        worker.close()


def test_offline_fallback_can_be_disabled_and_settings_validate():
    """Settings validate bounds and disabling fallback preserves provider_unavailable."""
    settings = Settings.from_env({
        'MALBUT_AGENT_OFFLINE_SAFETY_FALLBACK_ENABLED': 'false',
        'MALBUT_AGENT_OFFLINE_STOP_TIMEOUT_SECONDS': '4.5',
    })
    assert settings.offline_safety_fallback_enabled is False
    assert settings.offline_stop_timeout_seconds == 4.5
    settings.validate_for_dialogue()

    with pytest.raises(ValueError, match='MALBUT_AGENT_OFFLINE_STOP_TIMEOUT_SECONDS'):
        Settings(offline_stop_timeout_seconds=0.1).validate_for_dialogue()

    memory_store = SQLiteMemoryStore(':memory:')
    conversation_store = SQLiteConversationStore(':memory:')
    reliable = ReliableProvider(
        [_FailingProvider(error=NormalizedProviderError(ProviderFailureCode.NETWORK))],
        max_retries=0,
        sleep=lambda _s: None,
    )
    orchestrator = AgentOrchestrator(
        provider=reliable,
        memory_store=memory_store,
        conversation_store=conversation_store,
        safety_policy=SafetyPolicy(),
        offline_safety_fallback_enabled=False,
    )
    configure_speech_missions(orchestrator)
    try:
        session = conversation_store.create('speaker')
        res = orchestrator.handle(
            _speech_request('멈춰!', request_id='disabled-1', conversation_id=session.conversation_id)
        )
        assert res.decision.type == 'refusal'
        assert res.decision.reason == 'provider_unavailable'
    finally:
        conversation_store.close()
        memory_store.close()


@contextmanager
def _http_server_with_provider(provider):
    memory_store = SQLiteMemoryStore(':memory:')
    conversation_store = SQLiteConversationStore(':memory:')
    orchestrator = AgentOrchestrator(
        provider=provider,
        memory_store=memory_store,
        conversation_store=conversation_store,
        safety_policy=SafetyPolicy(),
    )
    server = make_server(
        '127.0.0.1',
        0,
        orchestrator,
        auth_token='health-token',
        allowed_user_id='http-user',
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f'http://{host}:{port}', orchestrator
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        conversation_store.close()
        memory_store.close()


def test_http_observability_exposes_provider_circuit_health_without_secrets():
    """GET /v1/observability/turns includes content-free provider circuit health."""
    failing = _FailingProvider(error=NormalizedProviderError(ProviderFailureCode.NETWORK))
    reliable = ReliableProvider(
        [failing],
        max_retries=0,
        failure_threshold=1,
        sleep=lambda _s: None,
    )
    with _http_server_with_provider(reliable) as (base_url, orch):
        # Initially closed / not degraded.
        assert orch.provider_health()['degraded'] is False

        # Trigger one failure via reliable provider to open circuit.
        session = orch.conversation_store.create('http-user')
        orch.handle(
            AgentRequest(
                request_id='http-fail-1',
                user_id='http-user',
                conversation_id=session.conversation_id,
                turn_id='t-1',
                utterance='안녕',
                robot_state=RobotState(),
                available_tools=(),
            )
        )

        req = urllib.request.Request(
            f'{base_url}/v1/observability/turns',
            headers={'Authorization': 'Bearer health-token'},
            method='GET',
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            payload = json.loads(resp.read())
        health = payload['provider_health']
        assert health['degraded'] is True
        assert health['circuits'][0]['state'] == 'open'
        assert health['circuits'][0]['consecutive_failures'] == 1
