"""Integration tests for the Mock multi-turn HTTP service."""

import json
import hashlib
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.gateway import (
    CapabilityRegistry,
    simulation_registry,
)
from malbut_agent_server.http_server import make_server
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.orchestrator import AgentOrchestrator
from malbut_agent_server.providers.base import AgentProvider
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.providers.openai_responses import (
    OpenAIResponsesProvider,
)
from malbut_agent_server.providers.reliable import ReliableProvider
from malbut_agent_server.robot_state import (
    RobotStateFieldEvidence,
    TrustedRobotStateEvidence,
)
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import AgentDecision, ProviderResult
from malbut_agent_server.speech import SpeechConversationCoordinator


SCRIPTED_AUTH_TOKEN = 'scripted-http-test-token-0123456789abcdef'


def _boottime_ns() -> int:
    clock_id = getattr(time, 'CLOCK_BOOTTIME', None)
    if clock_id is not None:
        return time.clock_gettime_ns(clock_id)
    return time.monotonic_ns()


class HTTPRobotStateSource:
    """Return one fixed current source-owned snapshot for HTTP tests."""

    def __init__(self, *, emergency_stop: bool = False) -> None:
        """Build lazily so the short validity interval starts at use."""
        self.emergency_stop = emergency_stop
        self.calls = 0
        self.evidence = None

    def read(self) -> TrustedRobotStateEvidence:
        """Return the same nonce-independent snapshot on exact retry."""
        self.calls += 1
        if self.evidence is not None:
            return self.evidence
        assembled = _boottime_ns()
        receipt = RobotStateFieldEvidence(
            source='test_ros_topic',
            received_boottime_ns=assembled,
        )
        self.evidence = TrustedRobotStateEvidence(
            evidence_digest=hashlib.sha256(
                b'http-trusted-robot-state'
            ).hexdigest(),
            device_id='http-monitor-device',
            map_id='http-monitor-map',
            map_revision='http-monitor-map-revision-1',
            host_boot_id='11111111-1111-4111-8111-111111111111',
            instance_id='22222222-2222-4222-8222-222222222222',
            sequence=1,
            assembled_at='2026-08-15T00:00:00+00:00',
            assembled_boottime_ns=assembled,
            valid_until_boottime_ns=assembled + 4_000_000_000,
            battery_percent=80.0,
            navigation_available=True,
            localization_ok=True,
            emergency_stop=self.emergency_stop,
            camera_available=True,
            privacy_mode=False,
            docked=False,
            forbidden_zones=(),
            field_evidence={
                name: receipt
                for name in (
                    'battery_percent',
                    'navigation_available',
                    'localization_ok',
                    'emergency_stop',
                    'camera_available',
                    'privacy_mode',
                    'docked',
                    'forbidden_zones',
                )
            },
        )
        return self.evidence


class BlockingProvider(AgentProvider):
    """Hold one model request until a shutdown test releases it."""

    def __init__(self) -> None:
        """Create deterministic request lifecycle signals."""
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(
        self,
        request,
        memories,
        conversation_turns,
        tools,
        conversation_summary=None,
    ) -> ProviderResult:
        """Wait, then return one bounded message proposal."""
        del request, memories, conversation_turns, tools
        del conversation_summary
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError('blocking provider was not released')
        return ProviderResult(
            decision=AgentDecision(type='message', message='종료 대기 완료'),
            provider='test',
            model='blocking-provider',
            latency_ms=1.0,
        )


@contextmanager
def running_server(
    auth_token: str = '',
    requests_per_minute: int = 60,
    failed_auth_attempts_per_minute: int = 30,
    provider: Optional[AgentProvider] = None,
    capability_registry: Optional[CapabilityRegistry] = None,
    scripted_speech: bool = False,
    trusted_robot_state_source=None,
    safety_policy: Optional[SafetyPolicy] = None,
) -> Iterator[str]:
    """Run one loopback-only ephemeral server."""
    memory_store = SQLiteMemoryStore(':memory:')
    conversation_store = SQLiteConversationStore(':memory:')
    orchestrator = AgentOrchestrator(
        provider=provider or MockProvider(),
        memory_store=memory_store,
        conversation_store=conversation_store,
        safety_policy=safety_policy or SafetyPolicy(),
        trusted_robot_state_source=trusted_robot_state_source,
        capability_registry=capability_registry,
    )
    speech_coordinator = (
        SpeechConversationCoordinator(orchestrator)
        if scripted_speech
        else None
    )
    server = make_server(
        '127.0.0.1',
        0,
        orchestrator,
        auth_token=auth_token,
        allowed_user_id='http-user',
        requests_per_minute=requests_per_minute,
        failed_auth_attempts_per_minute=(
            failed_auth_attempts_per_minute
        ),
        speech_coordinator=speech_coordinator,
    )
    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )
    thread.start()
    host, port = server.server_address
    try:
        yield f'http://{host}:{port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        conversation_store.close()
        memory_store.close()


def post(
    url: str,
    payload: dict,
    token: str = '',
) -> tuple:
    """POST JSON and decode success or HTTP error responses."""
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    request = urllib.request.Request(
        url,
        data=json.dumps(
            payload,
            ensure_ascii=False,
        ).encode('utf-8'),
        headers=headers,
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def get(url: str, token: str = '') -> tuple:
    """GET JSON and decode success or HTTP error responses."""
    headers = {}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    request = urllib.request.Request(url, headers=headers, method='GET')
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_health_endpoint() -> None:
    """The local server exposes a content-free health probe."""
    with running_server() as base_url:
        with urllib.request.urlopen(
            f'{base_url}/healthz',
            timeout=2,
        ) as response:
            health = json.loads(response.read())
        assert health == {
            'status': 'ok',
            'service': 'malbut_agent_server',
        }


def test_capability_discovery_is_authenticated_and_server_owned() -> None:
    """Discovery cannot bypass auth or expose executable side effects."""
    with running_server(auth_token='local-token') as base_url:
        status, error = get(f'{base_url}/v1/tools/capabilities')
        assert status == 401
        assert error['error']['code'] == 'unauthorized'

        status, result = get(
            f'{base_url}/v1/tools/capabilities',
            token='local-token',
        )
        assert status == 200
        assert result['source'] == 'server_owned_registry'
        assert all(
            item['executable'] is False
            for item in result['capabilities']
        )


def test_tool_query_runs_only_explicit_simulation_and_is_idempotent() -> None:
    """Query endpoint returns Mock evidence without real Tool effects."""
    with running_server(
        capability_registry=simulation_registry()
    ) as base_url:
        payload = {
            'request_id': 'http-tool-query-1',
            'user_id': 'http-user',
            'tool_name': 'navigate',
            'arguments': {'location': '거실'},
        }
        status, first = post(
            f'{base_url}/v1/tools/query',
            payload,
        )
        assert status == 200
        assert first['status'] == 'succeeded'
        assert first['result']['simulated'] is True
        assert first['result']['nav2_goal_published'] is False
        assert first['cached'] is False
        assert 'tool_call_id' not in first

        status, retry = post(
            f'{base_url}/v1/tools/query',
            payload,
        )
        assert status == 200
        assert retry['result_id'] == first['result_id']
        assert retry['cached'] is True


def test_tool_query_blocks_side_effect_and_fake_confirmation() -> None:
    """Proposal mode cannot be widened with client confirmation fields."""
    with running_server() as base_url:
        payload = {
            'request_id': 'blocked-navigation-1',
            'user_id': 'http-user',
            'tool_name': 'navigate',
            'arguments': {'location': '거실'},
        }
        status, blocked = post(
            f'{base_url}/v1/tools/query',
            payload,
        )
        assert status == 200
        assert blocked['status'] == 'rejected'
        assert blocked['error']['code'] == 'confirmation_required'
        assert 'tool_call_id' not in blocked

        status, error = post(
            f'{base_url}/v1/tools/query',
            {**payload, 'confirmation': True},
        )
        assert status == 400
        assert error['error']['code'] == 'validation_error'


def test_auth_token_is_required_when_configured() -> None:
    """Authentication is checked before mutation bodies are processed."""
    with running_server('local-token') as base_url:
        status, error = post(
            f'{base_url}/v1/agent/respond',
            {'not': 'processed'},
        )
        assert status == 401
        assert error['error']['code'] == 'unauthorized'

        status, error = post(
            f'{base_url}/v1/agent/respond',
            {'not': 'valid'},
            token='local-token',
        )
        assert status == 400
        assert error['error']['code'] == 'validation_error'


def test_failed_auth_has_a_separate_bounded_rate_limit() -> None:
    """Bad guesses are bounded without spending valid request slots."""
    with running_server(
        auth_token=SCRIPTED_AUTH_TOKEN,
        requests_per_minute=1,
        failed_auth_attempts_per_minute=2,
    ) as base_url:
        for _attempt in range(2):
            status, error = get(
                f'{base_url}/v1/tools/capabilities',
                token='wrong-token',
            )
            assert status == 401
            assert error['error']['code'] == 'unauthorized'

        status, error = get(
            f'{base_url}/v1/tools/capabilities',
            token='another-wrong-token',
        )
        assert status == 429
        assert error['error']['code'] == 'auth_rate_limited'

        status, _result = get(
            f'{base_url}/v1/tools/capabilities',
            token=SCRIPTED_AUTH_TOKEN,
        )
        assert status == 200

        status, error = get(
            f'{base_url}/v1/tools/capabilities',
            token=SCRIPTED_AUTH_TOKEN,
        )
        assert status == 429
        assert error['error']['code'] == 'rate_limited'


def _scripted_transcript(**overrides) -> dict:
    """Build one strict text-only transcript HTTP payload."""
    value = {
        'schema_version': 1,
        'utterance_id': 'scripted-utterance-1',
        'speech_session_id': 'scripted-session-1',
        'conversation_id': 'scripted-conversation-1',
        'sequence': 1,
        'capture_epoch': 1,
        'source_timestamp_ns': 1000000000,
        'text': '안녕',
        'confidence': 0.99,
        'is_final': True,
        'capture_origin': 'microphone',
        'audio_metadata': {
            'duration_ms': 500,
            'sample_rate_hz': 16000,
            'channel_count': 1,
        },
    }
    value.update(overrides)
    return value


def test_server_close_waits_for_active_scripted_request() -> None:
    """Keep SQLite available until every active handler finishes."""
    provider = BlockingProvider()
    memory_store = SQLiteMemoryStore(':memory:')
    conversation_store = SQLiteConversationStore(':memory:')
    orchestrator = AgentOrchestrator(
        provider=provider,
        memory_store=memory_store,
        conversation_store=conversation_store,
        safety_policy=SafetyPolicy(),
    )
    coordinator = SpeechConversationCoordinator(orchestrator)
    server = make_server(
        '127.0.0.1',
        0,
        orchestrator,
        auth_token=SCRIPTED_AUTH_TOKEN,
        allowed_user_id='http-user',
        speech_coordinator=coordinator,
    )
    serve_thread = threading.Thread(target=server.serve_forever)
    serve_thread.start()
    host, port = server.server_address
    base_url = f'http://{host}:{port}'
    request_result = {}
    close_started = threading.Event()
    close_finished = threading.Event()

    def run_request() -> None:
        request_result['value'] = post(
            f'{base_url}/v1/speech/scripted/transcripts',
            _scripted_transcript(),
            token=SCRIPTED_AUTH_TOKEN,
        )

    def close_server() -> None:
        close_started.set()
        server.server_close()
        close_finished.set()

    request_thread = threading.Thread(target=run_request)
    close_thread = threading.Thread(target=close_server)
    try:
        status, _opened = post(
            f'{base_url}/v1/speech/scripted/sessions/open',
            {
                'speech_session_id': 'scripted-session-1',
                'conversation_id': 'scripted-conversation-1',
            },
            token=SCRIPTED_AUTH_TOKEN,
        )
        assert status == 200
        request_thread.start()
        assert provider.started.wait(timeout=2)

        server.shutdown()
        serve_thread.join(timeout=2)
        assert not serve_thread.is_alive()
        close_thread.start()
        assert close_started.wait(timeout=1)
        assert close_finished.wait(timeout=0.1) is False
        assert request_thread.is_alive()

        provider.release.set()
        assert close_finished.wait(timeout=2)
        close_thread.join(timeout=2)
        request_thread.join(timeout=2)
        assert request_result['value'][0] == 200
    finally:
        provider.release.set()
        if serve_thread.is_alive():
            server.shutdown()
            serve_thread.join(timeout=2)
        if close_thread.is_alive():
            close_thread.join(timeout=2)
        server.server_close()
        if request_thread.is_alive():
            request_thread.join(timeout=2)
        conversation_store.close()
        memory_store.close()


def test_scripted_speech_endpoint_is_opt_in_and_requires_bearer() -> None:
    """The existing HTTP surface stays unchanged unless explicitly wired."""
    open_payload = {
        'speech_session_id': 'scripted-session-1',
        'conversation_id': 'scripted-conversation-1',
    }
    with running_server() as base_url:
        status, error = post(
            f'{base_url}/v1/speech/scripted/sessions/open',
            open_payload,
        )
        assert status == 404
        assert error['error']['code'] == 'not_found'

    with running_server(
        auth_token=SCRIPTED_AUTH_TOKEN,
        scripted_speech=True,
    ) as base_url:
        status, error = post(
            f'{base_url}/v1/speech/scripted/sessions/open',
            open_payload,
        )
        assert status == 401
        assert error['error']['code'] == 'unauthorized'


def test_scripted_speech_round_trip_is_text_only_and_non_actuating() -> None:
    """Run two authenticated turns without accepting client authority."""
    token = SCRIPTED_AUTH_TOKEN
    with running_server(
        auth_token=token,
        scripted_speech=True,
    ) as base_url:
        status, opened = post(
            f'{base_url}/v1/speech/scripted/sessions/open',
            {
                'speech_session_id': 'scripted-session-1',
                'conversation_id': 'scripted-conversation-1',
            },
            token=token,
        )
        assert status == 200
        assert opened['runtime'] == 'scripted_text_only'
        assert opened['physical_authority'] is False
        assert opened['binding'] == {
            'user_id': 'http-user',
            'speaker_id': 'scripted-http-user',
            'speech_session_id': 'scripted-session-1',
            'conversation_id': 'scripted-conversation-1',
            'source': 'scripted-http',
        }
        assert opened['result']['code'] == 'session_opened'

        status, rejected = post(
            f'{base_url}/v1/speech/scripted/transcripts',
            {
                **_scripted_transcript(),
                'robot_state': {'nav2_ready': True},
            },
            token=token,
        )
        assert status == 400
        assert rejected['error']['code'] == 'validation_error'

        status, first = post(
            f'{base_url}/v1/speech/scripted/transcripts',
            _scripted_transcript(),
            token=token,
        )
        assert status == 200
        assert first['runtime'] == 'scripted_text_only'
        assert first['physical_authority'] is False
        assert first['result']['status'] == 'responded'
        assert (
            first['result']['agent']['execution']['authorized']
            is False
        )
        tts_request = first['result']['tts_request']
        assert tts_request['text']

        status, terminal = post(
            f'{base_url}/v1/speech/scripted/tts/terminal',
            {
                'speech_session_id': 'scripted-session-1',
                'tts_request_id': tts_request['request_id'],
            },
            token=token,
        )
        assert status == 200
        assert terminal['result']['code'] == 'tts_terminal'
        assert terminal['result']['capture_epoch'] == 2

        status, second = post(
            f'{base_url}/v1/speech/scripted/transcripts',
            _scripted_transcript(
                utterance_id='scripted-utterance-2',
                sequence=2,
                capture_epoch=2,
                text='아까 내가 뭐라고 했지?',
            ),
            token=token,
        )
        assert status == 200
        assert second['result']['status'] == 'responded'
        assert '안녕' in second['result']['tts_request']['text']

        status, closed = post(
            f'{base_url}/v1/speech/scripted/sessions/close',
            {
                'speech_session_id': 'scripted-session-1',
                'control_id': 'scripted-close-1',
            },
            token=token,
        )
        assert status == 200
        assert closed['result']['status'] == 'closed'


def test_scripted_speech_cannot_turn_http_state_into_trusted_state() -> None:
    """Room monitoring remains blocked before Homecam target lookup."""
    token = SCRIPTED_AUTH_TOKEN
    with running_server(
        auth_token=token,
        scripted_speech=True,
    ) as base_url:
        status, _opened = post(
            f'{base_url}/v1/speech/scripted/sessions/open',
            {
                'speech_session_id': 'scripted-session-1',
                'conversation_id': 'scripted-conversation-1',
            },
            token=token,
        )
        assert status == 200

        status, response = post(
            f'{base_url}/v1/speech/scripted/transcripts',
            _scripted_transcript(text='거실 전체를 보여줘'),
            token=token,
        )
        assert status == 200
        assert response['physical_authority'] is False
        assert response['result']['status'] == 'responded'
        assert response['result']['confirmation_request'] is None
        agent = response['result']['agent']
        assert agent['decision']['type'] == 'refusal'
        assert agent['safety']['code'] == 'untrusted_robot_state'
        assert agent['execution']['state_trusted'] is False
        assert agent['execution']['authorized'] is False


def test_http_robot_state_is_ignored_and_cached_evidence_is_reverified(
) -> None:
    """Client JSON cannot override the fixed source in either direction."""
    source = HTTPRobotStateSource()
    with running_server(
        trusted_robot_state_source=source,
        safety_policy=SafetyPolicy(monitorable_locations=['거실']),
    ) as base_url:
        identity = {
            'user_id': 'http-user',
            'conversation_id': 'trusted-state-http',
        }
        assert post(f'{base_url}/v1/conversations', identity)[0] == 201
        request = {
            **identity,
            'request_id': 'trusted-state-http-request-1',
            'turn_id': 'trusted-state-http-turn-1',
            'utterance': '거실 전체를 보여줘',
            'robot_state': {
                'battery_percent': 0,
                'navigation_available': False,
                'localization_ok': False,
                'emergency_stop': True,
                'camera_available': False,
                'privacy_mode': True,
            },
            'available_tools': ['monitor_room'],
        }
        status, first = post(
            f'{base_url}/v1/agent/respond',
            request,
        )
        assert status == 200
        assert first['decision']['type'] == 'tool_call'
        assert first['safety']['allowed'] is True
        assert first['execution']['proposal_authorized'] is True
        assert first['execution']['state_evidence'] == {
            'scope': 'monitor_room',
            'evidence_digest': source.evidence.evidence_digest,
            'current': True,
        }
        assert 'http-monitor-device' not in json.dumps(first)

        status, replay = post(
            f'{base_url}/v1/agent/respond',
            request,
        )
        assert status == 200
        assert replay['decision']['type'] == 'tool_call'
        assert replay['execution']['proposal_authorized'] is True
        assert replay['execution']['decision_id'] == (
            first['execution']['decision_id']
        )
        assert source.calls == 2

    blocked_source = HTTPRobotStateSource(emergency_stop=True)
    with running_server(
        trusted_robot_state_source=blocked_source,
        safety_policy=SafetyPolicy(monitorable_locations=['거실']),
    ) as base_url:
        identity = {
            'user_id': 'http-user',
            'conversation_id': 'trusted-state-http-blocked',
        }
        assert post(f'{base_url}/v1/conversations', identity)[0] == 201
        status, blocked = post(
            f'{base_url}/v1/agent/respond',
            {
                **identity,
                'request_id': 'trusted-state-http-request-2',
                'turn_id': 'trusted-state-http-turn-2',
                'utterance': '거실 전체를 보여줘',
                'robot_state': {
                    'battery_percent': 100,
                    'navigation_available': True,
                    'localization_ok': True,
                    'emergency_stop': False,
                    'camera_available': True,
                    'privacy_mode': False,
                },
                'available_tools': ['monitor_room'],
            },
        )
        assert status == 200
        assert blocked['decision']['type'] == 'refusal'
        assert blocked['safety']['code'] == 'emergency_stop'
        assert blocked['execution']['proposal_authorized'] is False


def test_user_identity_is_bound_and_requests_are_rate_limited() -> None:
    """One server token cannot select arbitrary user namespaces."""
    with running_server(requests_per_minute=1) as base_url:
        status, error = post(
            f'{base_url}/v1/conversations',
            {
                'user_id': 'other-user',
                'conversation_id': 'blocked',
            },
        )
        assert status == 400
        assert error['error']['code'] == 'validation_error'

        status, error = post(
            f'{base_url}/v1/conversations',
            {
                'user_id': 'http-user',
                'conversation_id': 'rate-limited',
            },
        )
        assert status == 429
        assert error['error']['code'] == 'rate_limited'


def test_conversation_lifecycle_and_follow_up_round_trip() -> None:
    """HTTP exposes lifecycle, retry, follow-up, and ordered history."""
    with running_server() as base_url:
        identity = {
            'user_id': 'http-user',
            'conversation_id': 'conversation-lifecycle',
        }
        status, created = post(
            f'{base_url}/v1/conversations',
            identity,
        )
        assert status == 201
        assert created['conversation']['generation'] == 1

        first_request = {
            **identity,
            'request_id': 'lifecycle-request-1',
            'turn_id': 'turn-1',
            'utterance': '내 이름은 사용자A야',
            'robot_state': {},
            'available_tools': [],
        }
        status, first = post(
            f'{base_url}/v1/agent/respond',
            first_request,
        )
        assert status == 200

        status, duplicate = post(
            f'{base_url}/v1/agent/respond',
            first_request,
        )
        assert status == 200
        assert (
            duplicate['execution']['decision_id']
            == first['execution']['decision_id']
        )

        status, follow_up = post(
            f'{base_url}/v1/agent/respond',
            {
                **identity,
                'request_id': 'lifecycle-request-2',
                'turn_id': 'turn-2',
                'utterance': '아까 내가 뭐라고 했지?',
                'robot_state': {},
                'available_tools': [],
            },
        )
        assert status == 200
        assert '내 이름은 사용자A야' in follow_up['decision']['message']

        status, current = post(
            f'{base_url}/v1/conversations/get',
            identity,
        )
        assert status == 200
        assert len(current['turns']) == 2
        assert [
            message['role']
            for message in current['messages']
        ] == ['user', 'assistant', 'user', 'assistant']
        assert [
            message['sequence']
            for message in current['messages']
        ] == [1, 2, 3, 4]

        status, reset = post(
            f'{base_url}/v1/conversations/reset',
            identity,
        )
        assert status == 200
        assert reset['conversation']['generation'] == 2
        assert reset['messages'] == []

        status, closed = post(
            f'{base_url}/v1/conversations/close',
            identity,
        )
        assert status == 200
        assert closed['conversation']['status'] == 'closed'

        status, error = post(
            f'{base_url}/v1/agent/respond',
            {
                **identity,
                'request_id': 'lifecycle-request-3',
                'turn_id': 'turn-3',
                'utterance': '안녕',
                'robot_state': {},
                'available_tools': [],
            },
        )
        assert status == 409
        assert error['error']['code'] == 'conversation_state'

        status, deleted = post(
            f'{base_url}/v1/conversations/delete',
            identity,
        )
        assert status == 200
        assert deleted['deleted'] is True

        status, error = post(
            f'{base_url}/v1/conversations/get',
            identity,
        )
        assert status == 404
        assert error['error']['code'] == 'conversation_not_found'


def test_http_context_metrics_do_not_expose_conversation_content() -> None:
    """Public context telemetry contains sizes, never source text."""
    marker = 'HTTP_PRIVATE_CONTEXT_MARKER_42'
    with running_server() as base_url:
        identity = {
            'user_id': 'http-user',
            'conversation_id': 'context-metrics',
        }
        status, _ = post(
            f'{base_url}/v1/conversations',
            identity,
        )
        assert status == 201

        status, response = post(
            f'{base_url}/v1/agent/respond',
            {
                **identity,
                'request_id': 'context-metrics-request',
                'turn_id': 'context-metrics-turn',
                'utterance': marker,
                'robot_state': {},
                'available_tools': [],
            },
        )

        assert status == 200
        context = response['provider']['context']
        assert context['current_utterance']['source_chars'] == len(marker)
        assert context['current_utterance']['included_chars'] == len(marker)
        assert context['model_input']['chars'] <= context[
            'model_input'
        ]['max_chars']
        assert marker not in json.dumps(
            context,
            ensure_ascii=False,
        )


def test_provider_outage_returns_safe_refusal_and_server_survives() -> None:
    """A remote outage remains a non-action, not a server outage."""
    secret = 'sk-test-must-not-escape'

    def unavailable_transport(
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        timeout: int,
    ) -> Dict[str, Any]:
        del url, headers, payload, timeout
        raise TimeoutError(secret)

    remote = OpenAIResponsesProvider(
        api_key='test-key',
        model='offline-model',
        transport=unavailable_transport,
    )
    reliable = ReliableProvider(
        [remote],
        max_retries=0,
        base_delay_seconds=0,
        max_delay_seconds=0,
    )
    with running_server(provider=reliable) as base_url:
        identity = {
            'user_id': 'http-user',
            'conversation_id': 'outage-conversation',
        }
        status, _created = post(
            f'{base_url}/v1/conversations',
            identity,
        )
        assert status == 201

        for index in (1, 2):
            status, result = post(
                f'{base_url}/v1/agent/respond',
                {
                    **identity,
                    'request_id': f'outage-request-{index}',
                    'turn_id': f'turn-{index}',
                    'utterance': '안녕',
                    'robot_state': {},
                    'available_tools': [],
                },
            )
            assert status == 200
            assert result['decision']['type'] == 'refusal'
            assert (
                result['decision']['reason']
                == 'provider_unavailable'
            )
            assert (
                result['provider']['provider']
                == 'reliable-fallback'
            )
            assert result['execution']['authorized'] is False
            assert secret not in json.dumps(result)

        with urllib.request.urlopen(
            f'{base_url}/healthz',
            timeout=2,
        ) as response:
            health = json.loads(response.read())
        assert health['status'] == 'ok'
