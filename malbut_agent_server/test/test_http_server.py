"""Integration tests for the Mock multi-turn HTTP service."""

import json
import threading
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
from malbut_agent_server.safety import SafetyPolicy


@contextmanager
def running_server(
    auth_token: str = '',
    requests_per_minute: int = 60,
    provider: Optional[AgentProvider] = None,
    capability_registry: Optional[CapabilityRegistry] = None,
) -> Iterator[str]:
    """Run one loopback-only ephemeral server."""
    memory_store = SQLiteMemoryStore(':memory:')
    conversation_store = SQLiteConversationStore(':memory:')
    orchestrator = AgentOrchestrator(
        provider=provider or MockProvider(),
        memory_store=memory_store,
        conversation_store=conversation_store,
        safety_policy=SafetyPolicy(),
        capability_registry=capability_registry,
    )
    server = make_server(
        '127.0.0.1',
        0,
        orchestrator,
        auth_token=auth_token,
        allowed_user_id='http-user',
        requests_per_minute=requests_per_minute,
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
