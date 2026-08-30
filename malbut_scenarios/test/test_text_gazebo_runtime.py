"""Contracts for the concrete SWM25-133 acceptance runtime adapters."""

from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
import sqlite3
import threading

import pytest

from malbut_scenarios import text_gazebo_runtime as runtime_module
from malbut_scenarios.text_gazebo_evidence import ProductOutcome
from malbut_scenarios.text_gazebo_runtime import (
    ConcurrentApprovalResult,
    DuplicateRequestResult,
    LoopbackPortReservation,
    ProposalReceipt,
    SQLiteAcceptanceObserver,
    TextAgentHTTPClient,
    TextGazeboRuntimeError,
    installed_artifact_digest,
    loopback_listener_present,
    runtime_binding_digest,
    sanitized_ros_environment,
)
from malbut_scenarios.text_gazebo_scenario import TextGazeboScenarioProfile


_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_CONFIRMATION_ID = 'confirmation-private-133'


def _non_authorizing_execution() -> dict[str, object]:
    return {
        'authorized': False,
        'execution_authorized': False,
        'consume_once': False,
        'tool_call_id': None,
        'physical_authorized': False,
        'nav2_start_count': 0,
        'nav2_cancel_count': 0,
    }


@dataclass(frozen=True)
class _HTTPResponse:
    value: object = None
    status: int = 200
    raw: bytes | None = None
    content_type: str = 'application/json; charset=utf-8'
    declared_length: int | str | None = None

    def body(self) -> bytes:
        if self.raw is not None:
            return self.raw
        return json.dumps(
            self.value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(',', ':'),
        ).encode('utf-8')


class _ScriptedLoopbackServer:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.requests = []
        self._server = None
        self._thread = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.server_address[1])

    def __enter__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self._handle()

            def do_POST(self):
                self._handle()

            def _handle(self):
                declared = self.headers.get('Content-Length')
                length = 0 if declared is None else int(declared)
                body = self.rfile.read(length)
                owner.requests.append({
                    'method': self.command,
                    'path': self.path,
                    'headers': dict(self.headers.items()),
                    'body': body,
                })
                if owner.responses:
                    response = owner.responses.pop(0)
                else:
                    response = _HTTPResponse(
                        status=500,
                        value={'error': 'script_exhausted'},
                    )
                payload = response.body()
                self.send_response(response.status)
                self.send_header('Content-Type', response.content_type)
                content_length = response.declared_length
                if content_length is None:
                    content_length = len(payload)
                self.send_header('Content-Length', str(content_length))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            def log_message(self, _format, *_args):
                return None

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name='swm25-133-test-http',
            daemon=False,
        )
        self._thread.start()
        return self

    def __exit__(self, *_args):
        assert self._server is not None
        assert self._thread is not None
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)
        assert not self._thread.is_alive()


def _client(port: int) -> TextAgentHTTPClient:
    return TextAgentHTTPClient(
        port,
        token='private-agent-token',
        user_id='private-user',
        run_nonce='0123456789abcdef0123456789abcdef',
        timeout_seconds=1.0,
    )


def test_http_client_drives_exact_public_happy_path_contract() -> None:
    responses = [
        _HTTPResponse(value={
            'status': 'ok',
            'service': 'malbut_agent_server',
        }),
        _HTTPResponse(status=201, value={'conversation': {
            'conversation_id': (
                'swm25-133-0123456789abcdef0123456789abcdef'
            ),
            'user_id': 'private-user',
            'generation': 1,
        }}),
        _HTTPResponse(value={
            'status': 'awaiting_confirmation',
            'result_code': 'confirmation_pending',
            'cached': False,
            'proposal': {
                'tool_name': 'navigate',
                'arguments': {'location': '거실'},
            },
            'confirmation_request_id': _CONFIRMATION_ID,
            'execution': _non_authorizing_execution(),
        }),
        _HTTPResponse(value={
            'status': 'approved',
            'result_code': 'confirmation_approved',
            'cached': False,
            'execution': _non_authorizing_execution(),
        }),
        _HTTPResponse(value={
            'status': 'approved',
            'result_code': 'confirmation_approved',
            'cached': True,
            'execution': _non_authorizing_execution(),
        }),
        _HTTPResponse(value={
            'status': 'no_pending_confirmation',
            'result_code': 'confirmation_not_pending',
            'execution': _non_authorizing_execution(),
        }),
    ]
    with _ScriptedLoopbackServer(responses) as server:
        client = _client(server.port)
        client.await_health(1.0)
        client.create_conversation()
        receipt = client.request_navigation()
        client.approve_navigation()
        client.replay_approval()
        client.send_late_approval()

    assert receipt.confirmation_request_id == _CONFIRMATION_ID
    assert [request['method'] for request in server.requests] == [
        'GET', 'POST', 'POST', 'POST', 'POST', 'POST',
    ]
    assert [request['path'] for request in server.requests] == [
        '/healthz',
        '/v1/conversations',
        '/v1/text/turns',
        '/v1/text/turns',
        '/v1/text/turns',
        '/v1/text/turns',
    ]
    assert 'Authorization' not in server.requests[0]['headers']
    for request in server.requests[1:]:
        assert request['headers']['Authorization'] == (
            'Bearer private-agent-token'
        )
        assert request['headers']['Content-Type'] == 'application/json'
    request_values = [
        json.loads(request['body'].decode('utf-8'))
        for request in server.requests[1:]
    ]
    assert request_values[1]['text'] == '거실로 가줘'
    assert request_values[2]['text'] == '네'
    assert request_values[2] == request_values[3]
    assert request_values[4]['request_id'].startswith('late-')


def test_http_client_exact_request_replay_reuses_confirmation_binding(
) -> None:
    proposal = {
        'status': 'awaiting_confirmation',
        'result_code': 'confirmation_pending',
        'cached': False,
        'proposal': {
            'tool_name': 'navigate',
            'arguments': {'location': '거실'},
        },
        'confirmation_request_id': _CONFIRMATION_ID,
        'execution': _non_authorizing_execution(),
    }
    with _ScriptedLoopbackServer([
        _HTTPResponse(value=proposal),
        _HTTPResponse(value={**proposal, 'cached': True}),
    ]) as server:
        client = _client(server.port)
        receipt = client.request_navigation()
        result = client.replay_navigation_request(receipt)

    assert result == DuplicateRequestResult(
        request_attempt_count=2,
        matching_confirmation_count=2,
        additional_confirmation_binding_count=0,
    )
    assert len(server.requests) == 2
    first = json.loads(server.requests[0]['body'].decode('utf-8'))
    second = json.loads(server.requests[1]['body'].decode('utf-8'))
    assert first == second
    rendered = repr(result)
    assert _CONFIRMATION_ID not in rendered
    assert 'private-agent-token' not in rendered
    assert '거실로 가줘' not in rendered


def test_http_client_request_replay_rejects_changed_binding_safely() -> None:
    proposal = {
        'status': 'awaiting_confirmation',
        'result_code': 'confirmation_pending',
        'cached': False,
        'proposal': {
            'tool_name': 'navigate',
            'arguments': {'location': '거실'},
        },
        'confirmation_request_id': _CONFIRMATION_ID,
        'execution': _non_authorizing_execution(),
    }
    with _ScriptedLoopbackServer([
        _HTTPResponse(value=proposal),
        _HTTPResponse(value={
            **proposal,
            'confirmation_request_id': 'changed-private-binding',
        }),
    ]) as server:
        client = _client(server.port)
        receipt = client.request_navigation()
        with pytest.raises(TextGazeboRuntimeError) as caught:
            client.replay_navigation_request(receipt)

    assert caught.value.code == 'agent_duplicate_request_invalid'
    rendered = repr(caught.value)
    assert _CONFIRMATION_ID not in rendered
    assert 'changed-private-binding' not in rendered
    assert 'private-agent-token' not in rendered


def test_http_client_concurrent_approvals_have_one_winner_and_safe_loser(
) -> None:
    loser = _HTTPResponse(status=409, value={'error': {
        'code': 'confirmation_already_terminal',
        'message': 'confirmation intent is already terminal',
    }})
    approved = _HTTPResponse(value={
        'status': 'approved',
        'result_code': 'confirmation_approved',
        'cached': False,
        'execution': _non_authorizing_execution(),
    })
    cached = _HTTPResponse(value={
        'status': 'approved',
        'result_code': 'confirmation_approved',
        'cached': True,
        'execution': _non_authorizing_execution(),
    })
    with _ScriptedLoopbackServer([approved, loser, cached]) as server:
        client = _client(server.port)
        result = client.approve_navigation_concurrently()
        client.replay_winning_approval(result)

    assert result == ConcurrentApprovalResult(
        approval_attempt_count=2,
        approved_count=1,
        non_authorizing_loser_count=1,
    )
    assert len(server.requests) == 3
    values = [
        json.loads(request['body'].decode('utf-8'))
        for request in server.requests
    ]
    assert len({value['request_id'] for value in values[:2]}) == 2
    assert len({value['turn_id'] for value in values[:2]}) == 2
    canonical = [
        json.dumps(value, ensure_ascii=False, sort_keys=True)
        for value in values
    ]
    assert sorted(canonical.count(value) for value in set(canonical)) == [1, 2]
    assert {value['text'] for value in values} == {'네'}
    rendered = repr(result)
    assert 'private-agent-token' not in rendered
    assert '0123456789abcdef' not in rendered
    assert not any(
        thread.name.startswith('swm25-136-approval-')
        for thread in threading.enumerate()
    )


def test_concurrent_approval_rejects_a_late_no_pending_loser() -> None:
    """A serialized late request cannot prove the intended stale-CAS race."""
    approved = _HTTPResponse(value={
        'status': 'approved',
        'result_code': 'confirmation_approved',
        'cached': False,
        'execution': _non_authorizing_execution(),
    })
    late = _HTTPResponse(value={
        'status': 'no_pending_confirmation',
        'result_code': 'confirmation_not_pending',
        'execution': _non_authorizing_execution(),
    })
    with _ScriptedLoopbackServer([approved, late]) as server:
        client = _client(server.port)
        with pytest.raises(TextGazeboRuntimeError) as caught:
            client.approve_navigation_concurrently()

    assert caught.value.code == 'agent_concurrent_approval_invalid'


def test_http_client_rejects_foreign_concurrent_result_before_io() -> None:
    result = ConcurrentApprovalResult(
        approval_attempt_count=2,
        approved_count=1,
        non_authorizing_loser_count=1,
    )
    with _ScriptedLoopbackServer([]) as server:
        client = _client(server.port)
        with pytest.raises(ValueError, match='result is invalid'):
            client.replay_winning_approval(result)

    assert server.requests == []


def test_http_client_concurrent_approvals_fail_closed_without_one_loser(
) -> None:
    approved = _HTTPResponse(value={
        'status': 'approved',
        'result_code': 'confirmation_approved',
        'cached': False,
        'execution': _non_authorizing_execution(),
    })
    with _ScriptedLoopbackServer([approved, approved]) as server:
        client = _client(server.port)
        with pytest.raises(TextGazeboRuntimeError) as caught:
            client.approve_navigation_concurrently()

    assert caught.value.code == 'agent_concurrent_approval_invalid'
    assert 'private-agent-token' not in repr(caught.value)
    assert '0123456789abcdef' not in repr(caught.value)
    assert not any(
        thread.name.startswith('swm25-136-approval-')
        for thread in threading.enumerate()
    )


@pytest.mark.parametrize(
    'profile,request_text,location',
    (
        (TextGazeboScenarioProfile.HAPPY_KITCHEN, '주방으로 가줘', '주방'),
        (TextGazeboScenarioProfile.HAPPY_BEDROOM, '침실로 가줘', '침실'),
    ),
)
def test_http_client_binds_request_and_proposal_to_profile(
    profile, request_text, location,
) -> None:
    response = _HTTPResponse(value={
        'status': 'awaiting_confirmation',
        'result_code': 'confirmation_pending',
        'cached': False,
        'proposal': {
            'tool_name': 'navigate',
            'arguments': {'location': location},
        },
        'confirmation_request_id': _CONFIRMATION_ID,
        'execution': _non_authorizing_execution(),
    })
    with _ScriptedLoopbackServer([response]) as server:
        client = TextAgentHTTPClient(
            server.port,
            token='private-agent-token',
            user_id='private-user',
            run_nonce='0123456789abcdef0123456789abcdef',
            timeout_seconds=1.0,
            scenario_profile=profile,
        )
        client.request_navigation()

    request = json.loads(server.requests[0]['body'].decode('utf-8'))
    assert request['text'] == request_text


def test_http_client_rejects_unknown_profile_before_io() -> None:
    with pytest.raises(ValueError, match='not allowlisted'):
        TextAgentHTTPClient(
            8877,
            token='token',
            user_id='user',
            run_nonce='nonce',
            scenario_profile='거실',
        )


@pytest.mark.parametrize(
    'response,operation,code',
    (
        (
            _HTTPResponse(
                value={'conversation': {}},
                content_type='text/plain',
            ),
            'create',
            'agent_http_response_invalid',
        ),
        (
            _HTTPResponse(
                raw=b'{"conversation":{},"conversation":{}}',
            ),
            'create',
            'agent_http_response_invalid',
        ),
        (
            _HTTPResponse(
                value={},
                declared_length=1_000_001,
            ),
            'create',
            'agent_http_response_invalid',
        ),
        (
            _HTTPResponse(value={
                'status': 'awaiting_confirmation',
                'result_code': 'confirmation_pending',
                'cached': False,
                'proposal': {
                    'tool_name': 'navigate',
                    'arguments': {'location': '침실'},
                },
                'confirmation_request_id': _CONFIRMATION_ID,
                'execution': _non_authorizing_execution(),
            }),
            'proposal',
            'agent_proposal_invalid',
        ),
        (
            _HTTPResponse(value={
                'status': 'approved',
                'result_code': 'confirmation_approved',
                'cached': False,
                'execution': {
                    **_non_authorizing_execution(),
                    'nav2_start_count': 1,
                },
            }),
            'approval',
            'agent_approval_invalid',
        ),
    ),
)
def test_http_client_fails_closed_on_invalid_response_contract(
    response,
    operation,
    code,
) -> None:
    with _ScriptedLoopbackServer([response]) as server:
        client = _client(server.port)
        with pytest.raises(TextGazeboRuntimeError) as caught:
            if operation == 'create':
                client.create_conversation()
            elif operation == 'proposal':
                client.request_navigation()
            else:
                client.approve_navigation()

    assert caught.value.code == code
    assert str(caught.value) == code
    assert 'private-agent-token' not in repr(caught.value)


def test_http_client_construction_has_zero_io_and_unavailable_is_bounded(
) -> None:
    """Construct offline, then normalize one explicit connection failure."""
    with LoopbackPortReservation() as reservation:
        client = _client(reservation.port)
        assert repr(client) == 'TextAgentHTTPClient(configured=True)'
        with pytest.raises(TextGazeboRuntimeError) as caught:
            client.create_conversation()

    assert caught.value.code == 'agent_http_unavailable'
    assert str(reservation.port) not in repr(caught.value)


@pytest.mark.parametrize(
    'changes',
    (
        {'port': 0},
        {'port': True},
        {'token': ''},
        {'token': 'line\nbreak'},
        {'user_id': ''},
        {'run_nonce': 'x' * 65},
        {'timeout_seconds': 0},
        {'timeout_seconds': float('inf')},
    ),
)
def test_http_client_rejects_invalid_configuration_before_io(changes) -> None:
    values = {
        'port': 8877,
        'token': 'token',
        'user_id': 'user',
        'run_nonce': 'nonce',
        'timeout_seconds': 1.0,
    }
    values.update(changes)
    with pytest.raises(ValueError):
        TextAgentHTTPClient(**values)


def _create_ledger(
    database: Path,
    *,
    confirmation_state: str = 'pending',
    confirmation_disposition: str = 'pending',
    confirmation_result: str = 'confirmation_pending',
    action_state: str | None = None,
    action_result: str | None = None,
    outbox_state: str | None = None,
    outbox_result: str | None = None,
) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.executescript('''
            CREATE TABLE conversation_turns (
                turn_id TEXT PRIMARY KEY,
                status TEXT NOT NULL
            );
            CREATE TABLE confirmation_intents (
                confirmation_request_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                disposition TEXT NOT NULL,
                result_code TEXT NOT NULL
            );
            CREATE TABLE robot_actions (
                action_id TEXT PRIMARY KEY,
                confirmation_request_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,
                result_code TEXT,
                simulation INTEGER NOT NULL CHECK (simulation = 1),
                physical_authorized INTEGER NOT NULL
                    CHECK (physical_authorized = 0)
            );
            CREATE TABLE execution_outbox (
                action_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,
                result_code TEXT,
                simulation INTEGER NOT NULL CHECK (simulation = 1),
                physical_authorized INTEGER NOT NULL
                    CHECK (physical_authorized = 0)
            );
        ''')
        connection.execute(
            "INSERT INTO conversation_turns VALUES (?, 'completed')",
            ('private-agent-turn',),
        )
        connection.execute(
            'INSERT INTO confirmation_intents VALUES (?, ?, ?, ?)',
            (
                _CONFIRMATION_ID,
                confirmation_state,
                confirmation_disposition,
                confirmation_result,
            ),
        )
        if action_state is not None:
            connection.execute(
                'INSERT INTO robot_actions VALUES (?, ?, ?, ?, 1, 0)',
                (
                    'private-action-id',
                    _CONFIRMATION_ID,
                    action_state,
                    action_result,
                ),
            )
        if outbox_state is not None:
            connection.execute(
                'INSERT INTO execution_outbox VALUES (?, ?, ?, 1, 0)',
                (
                    'private-action-id',
                    outbox_state,
                    outbox_result,
                ),
            )
        connection.commit()
    finally:
        connection.close()


def test_sqlite_observer_projects_exact_preapproval_without_writes(
    monkeypatch,
    tmp_path,
) -> None:
    database = (tmp_path / 'acceptance.sqlite3').resolve()
    _create_ledger(database)
    before = database.stat()
    connect_calls = []
    real_connect = sqlite3.connect

    def read_only_connect(target, *args, **kwargs):
        connect_calls.append((target, kwargs.copy()))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(runtime_module.sqlite3, 'connect', read_only_connect)
    observer = SQLiteAcceptanceObserver(database)
    snapshot = observer.snapshot(_CONFIRMATION_ID)
    after = database.stat()

    assert snapshot.is_preapproval() is True
    assert snapshot.is_known_success() is False
    assert snapshot.robot_action_count == 0
    assert snapshot.dispatch_intent_count == 0
    assert snapshot.durable_agent_turn_count == 1
    assert before.st_size == after.st_size
    assert before.st_mtime_ns == after.st_mtime_ns
    assert connect_calls
    assert all('?mode=ro' in str(call[0]) for call in connect_calls)
    assert all(call[1].get('uri') is True for call in connect_calls)


def test_sqlite_observer_accepts_only_exact_known_success(tmp_path) -> None:
    database = (tmp_path / 'acceptance.sqlite3').resolve()
    _create_ledger(
        database,
        confirmation_state='resolved',
        confirmation_disposition='approved',
        confirmation_result='confirmation_approved',
        action_state='SUCCEEDED',
        action_result='NAVIGATION_SUCCEEDED',
        outbox_state='TERMINAL',
        outbox_result='NAVIGATION_SUCCEEDED',
    )
    observer = SQLiteAcceptanceObserver(database)

    snapshot = observer.await_known_success(
        _CONFIRMATION_ID,
        timeout_seconds=0.1,
        poll_seconds=0.01,
    )

    assert snapshot.is_known_success() is True
    assert snapshot.approved_confirmation_count == 1
    assert snapshot.robot_action_count == 1
    assert snapshot.dispatch_intent_count == 1
    assert snapshot.durable_agent_turn_count == 1
    assert snapshot.simulation is True
    assert snapshot.physical_authorized is False
    assert observer.quick_check() is True


@pytest.mark.parametrize(
    'result_code',
    (
        'robot_state_stale',
        'safety_emergency_stop',
        'target_binding_changed',
    ),
)
def test_sqlite_observer_accepts_exact_block_without_dispatch(
    tmp_path,
    result_code,
) -> None:
    database = (tmp_path / f'{result_code}.sqlite3').resolve()
    _create_ledger(
        database,
        confirmation_state='resolved',
        confirmation_disposition='approved',
        confirmation_result='confirmation_approved',
        action_state='BLOCKED',
        action_result=result_code,
    )
    observer = SQLiteAcceptanceObserver(database)

    snapshot = observer.await_expected_blocked(
        _CONFIRMATION_ID,
        result_code=result_code,
        timeout_seconds=0.1,
        poll_seconds=0.01,
    )

    assert snapshot.is_expected_blocked(result_code) is True
    assert snapshot.robot_action_count == 1
    assert snapshot.dispatch_intent_count == 0
    assert snapshot.dispatch_state is None
    assert snapshot.simulation is True
    assert snapshot.physical_authorized is False


@pytest.mark.parametrize(
    'result_code',
    (
        'navigation_start_outcome_unknown',
        'navigation_status_outcome_unknown',
        'dispatch_outcome_unknown_after_restart',
    ),
)
def test_sqlite_observer_accepts_only_matching_action_outbox_unknown(
    tmp_path,
    result_code,
) -> None:
    database = (tmp_path / f'{result_code}.sqlite3').resolve()
    _create_ledger(
        database,
        confirmation_state='resolved',
        confirmation_disposition='approved',
        confirmation_result='confirmation_approved',
        action_state='UNKNOWN',
        action_result=result_code,
        outbox_state='UNKNOWN',
        outbox_result=result_code,
    )
    observer = SQLiteAcceptanceObserver(database)

    snapshot = observer.await_expected_unknown(
        _CONFIRMATION_ID,
        result_code=result_code,
        timeout_seconds=0.1,
        poll_seconds=0.01,
    )

    assert snapshot.is_expected_unknown(result_code) is True
    assert snapshot.robot_action_count == 1
    assert snapshot.dispatch_intent_count == 1
    assert snapshot.dispatch_state == 'UNKNOWN'
    assert snapshot.simulation is True
    assert snapshot.physical_authorized is False


def test_sqlite_observer_rejects_divergent_unknown_action_and_outbox(
    tmp_path,
) -> None:
    database = (tmp_path / 'divergent-unknown.sqlite3').resolve()
    _create_ledger(
        database,
        confirmation_state='resolved',
        confirmation_disposition='approved',
        confirmation_result='confirmation_approved',
        action_state='UNKNOWN',
        action_result='navigation_start_outcome_unknown',
        outbox_state='UNKNOWN',
        outbox_result='navigation_status_outcome_unknown',
    )

    snapshot = SQLiteAcceptanceObserver(database).snapshot(
        _CONFIRMATION_ID
    )

    assert snapshot.is_expected_unknown(
        'navigation_start_outcome_unknown'
    ) is False


def test_sqlite_observer_does_not_accept_wrong_block_or_outbox(
    tmp_path,
) -> None:
    wrong = (tmp_path / 'wrong-code.sqlite3').resolve()
    _create_ledger(
        wrong,
        confirmation_state='resolved',
        confirmation_disposition='approved',
        confirmation_result='confirmation_approved',
        action_state='BLOCKED',
        action_result='safety_emergency_stop',
    )
    with pytest.raises(TextGazeboRuntimeError) as mismatch:
        SQLiteAcceptanceObserver(wrong).await_expected_blocked(
            _CONFIRMATION_ID,
            result_code='robot_state_stale',
            timeout_seconds=0.1,
            poll_seconds=0.01,
        )
    assert mismatch.value.code == 'ledger_terminal_failed'

    dispatched = (tmp_path / 'dispatched.sqlite3').resolve()
    _create_ledger(
        dispatched,
        confirmation_state='resolved',
        confirmation_disposition='approved',
        confirmation_result='confirmation_approved',
        action_state='BLOCKED',
        action_result='robot_state_stale',
        outbox_state='PENDING',
    )
    snapshot = SQLiteAcceptanceObserver(dispatched).snapshot(
        _CONFIRMATION_ID
    )
    assert snapshot.is_expected_blocked('robot_state_stale') is False


def test_sqlite_observer_requires_typed_outcome_and_exact_block_code(
    tmp_path,
) -> None:
    database = (tmp_path / 'pending.sqlite3').resolve()
    _create_ledger(database)
    observer = SQLiteAcceptanceObserver(database)

    with pytest.raises(TypeError, match='ProductOutcome'):
        observer.await_product_outcome(
            _CONFIRMATION_ID,
            expected_product_outcome='blocked',
            expected_block_result_code='robot_state_stale',
            timeout_seconds=0.1,
        )
    with pytest.raises(ValueError, match='block result code'):
        observer.await_product_outcome(
            _CONFIRMATION_ID,
            expected_product_outcome=ProductOutcome.BLOCKED,
            expected_block_result_code='private/value',
            timeout_seconds=0.1,
        )
    with pytest.raises(ValueError, match='cannot require'):
        observer.await_product_outcome(
            _CONFIRMATION_ID,
            expected_product_outcome=ProductOutcome.SUCCEEDED,
            expected_block_result_code='robot_state_stale',
            timeout_seconds=0.1,
        )
    with pytest.raises(ValueError, match='result code'):
        observer.await_product_outcome(
            _CONFIRMATION_ID,
            expected_product_outcome=ProductOutcome.UNKNOWN,
            expected_unknown_result_code='private/value',
            timeout_seconds=0.1,
        )
    with pytest.raises(ValueError, match='cannot require'):
        observer.await_product_outcome(
            _CONFIRMATION_ID,
            expected_product_outcome=ProductOutcome.UNKNOWN,
            expected_block_result_code='robot_state_stale',
            expected_unknown_result_code=(
                'navigation_start_outcome_unknown'
            ),
            timeout_seconds=0.1,
        )


@pytest.mark.parametrize(
    'action_state',
    ['FAILED', 'CANCELED', 'BLOCKED', 'UNKNOWN'],
)
def test_sqlite_observer_rejects_known_terminal_failure(
    tmp_path,
    action_state,
) -> None:
    database = (tmp_path / f'{action_state}.sqlite3').resolve()
    _create_ledger(
        database,
        confirmation_state='resolved',
        confirmation_disposition='approved',
        confirmation_result='confirmation_approved',
        action_state=action_state,
        action_result='NAVIGATION_NOT_SUCCESSFUL',
        outbox_state='TERMINAL',
        outbox_result='NAVIGATION_NOT_SUCCESSFUL',
    )
    observer = SQLiteAcceptanceObserver(database)

    with pytest.raises(TextGazeboRuntimeError) as caught:
        observer.await_known_success(
            _CONFIRMATION_ID,
            timeout_seconds=0.1,
            poll_seconds=0.01,
        )

    assert caught.value.code == 'ledger_terminal_failed'


def test_sqlite_observer_times_out_pending_and_never_creates_missing_db(
    tmp_path,
) -> None:
    database = (tmp_path / 'pending.sqlite3').resolve()
    _create_ledger(database)
    observer = SQLiteAcceptanceObserver(database)
    with pytest.raises(TextGazeboRuntimeError) as caught:
        observer.await_known_success(
            _CONFIRMATION_ID,
            timeout_seconds=0.02,
            poll_seconds=0.005,
        )
    assert caught.value.code == 'ledger_terminal_timeout'

    missing = (tmp_path / 'missing.sqlite3').resolve()
    missing_observer = SQLiteAcceptanceObserver(missing)
    with pytest.raises(TextGazeboRuntimeError) as missing_error:
        missing_observer.snapshot(_CONFIRMATION_ID)
    assert missing_error.value.code == 'ledger_unavailable'
    assert not missing.exists()


def test_sqlite_observer_rejects_wrong_or_ambiguous_confirmation(
    tmp_path,
) -> None:
    database = (tmp_path / 'ambiguous.sqlite3').resolve()
    _create_ledger(database)
    observer = SQLiteAcceptanceObserver(database)

    with pytest.raises(TextGazeboRuntimeError) as wrong:
        observer.snapshot('another-confirmation')
    assert wrong.value.code == 'ledger_snapshot_invalid'

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            'INSERT INTO confirmation_intents VALUES (?, ?, ?, ?)',
            ('second-confirmation', 'pending', 'pending',
             'confirmation_pending'),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(TextGazeboRuntimeError) as ambiguous:
        observer.snapshot(_CONFIRMATION_ID)
    assert ambiguous.value.code == 'ledger_snapshot_invalid'


def test_sqlite_observer_rejects_duplicate_durable_agent_turn(
    tmp_path,
) -> None:
    database = (tmp_path / 'duplicate-turn.sqlite3').resolve()
    _create_ledger(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO conversation_turns VALUES (?, 'completed')",
            ('second-private-agent-turn',),
        )
        connection.commit()
    finally:
        connection.close()

    snapshot = SQLiteAcceptanceObserver(database).snapshot(
        _CONFIRMATION_ID
    )

    assert snapshot.durable_agent_turn_count == 2
    assert snapshot.is_preapproval() is False
    assert snapshot.is_known_success() is False


def test_loopback_port_reservation_holds_then_releases_one_port() -> None:
    reservation = LoopbackPortReservation()
    port = reservation.port
    contender = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError):
            contender.bind(('127.0.0.1', port))
        assert loopback_listener_present(port) is False
    finally:
        contender.close()
        reservation.release()
        reservation.release()

    replacement = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        replacement.bind(('127.0.0.1', port))
    finally:
        replacement.close()


def test_loopback_listener_detects_fake_agent_server() -> None:
    with _ScriptedLoopbackServer([]) as server:
        assert loopback_listener_present(server.port) is True
    assert loopback_listener_present(server.port) is False


@pytest.mark.parametrize('port', [0, 65536, -1, True, '8877'])
def test_loopback_listener_rejects_invalid_port(port) -> None:
    with pytest.raises(ValueError, match='loopback port'):
        loopback_listener_present(port)


def test_runtime_binding_digest_is_deterministic_private_and_bound() -> None:
    private_values = {
        'device_id': 'private-device-01',
        'map_id': 'private-map-small-house',
        'map_revision': 'private-revision-7',
    }
    first = runtime_binding_digest(**private_values)
    second = runtime_binding_digest(**private_values)
    changed = runtime_binding_digest(
        **{**private_values, 'map_revision': 'private-revision-8'}
    )

    assert _DIGEST.fullmatch(first)
    assert first == second
    assert changed != first
    assert all(value not in first for value in private_values.values())


@pytest.mark.parametrize(
    'changes',
    (
        {'device_id': ''},
        {'map_id': 'line\nbreak'},
        {'map_revision': 'x' * 257},
        {'device_id': 123},
    ),
)
def test_runtime_binding_rejects_invalid_private_identity(changes) -> None:
    values = {
        'device_id': 'device',
        'map_id': 'map',
        'map_revision': 'revision',
    }
    values.update(changes)
    with pytest.raises(TextGazeboRuntimeError) as caught:
        runtime_binding_digest(**values)
    assert caught.value.code == 'runtime_binding_invalid'
    assert all(
        str(value) not in repr(caught.value)
        for value in changes.values()
        if str(value)
    )


def test_installed_artifact_digest_binds_labels_bytes_and_not_paths(
    tmp_path,
) -> None:
    first = (tmp_path / 'first.py').resolve()
    second = (tmp_path / 'second.yaml').resolve()
    first.write_bytes(b'print("installed")\n')
    second.write_bytes(b'answer: 42\n')

    digest = installed_artifact_digest({
        'scenario': first,
        'config': second,
    })
    reordered = installed_artifact_digest({
        'config': second,
        'scenario': first,
    })
    relabeled = installed_artifact_digest({
        'config': first,
        'scenario': second,
    })

    assert _DIGEST.fullmatch(digest)
    assert reordered == digest
    assert relabeled != digest
    assert str(tmp_path) not in digest
    assert first.name not in digest
    assert second.name not in digest


@pytest.mark.parametrize('kind', ['empty', 'missing', 'symlink'])
def test_installed_artifact_digest_rejects_untrusted_file(
    tmp_path,
    kind,
) -> None:
    candidate = (tmp_path / kind).resolve()
    if kind == 'empty':
        candidate.write_bytes(b'')
    elif kind == 'symlink':
        target = tmp_path / 'target'
        target.write_bytes(b'content')
        candidate.symlink_to(target)

    with pytest.raises(TextGazeboRuntimeError) as caught:
        installed_artifact_digest({'artifact': candidate})

    assert caught.value.code == 'installed_artifact_invalid'
    assert str(candidate) not in repr(caught.value)


def test_sanitized_ros_environment_is_allowlisted_and_content_free(
    tmp_path,
) -> None:
    private_home = (tmp_path / 'private-home').resolve()
    source = {
        'PATH': '/trusted/bin',
        'PYTHONPATH': '/trusted/python',
        'ROS_DISTRO': 'humble',
        'DISPLAY': ':99',
        'XAUTHORITY': '/private/xauth',
        'HOME': '/home/operator',
        'ROS_DOMAIN_ID': '0',
        'OPENAI_API_KEY': 'private-openai-secret',
        'MALBUT_AGENT_AUTH_TOKEN': 'private-agent-secret',
        'AWS_SECRET_ACCESS_KEY': 'private-aws-secret',
        'LD_PRELOAD': '/private/injected.so',
    }

    headless = sanitized_ros_environment(
        source,
        private_home=private_home,
        domain_id=73,
        gui=False,
    )
    gui = sanitized_ros_environment(
        source,
        private_home=private_home,
        domain_id=74,
        gui=True,
    )

    assert headless == {
        'PATH': '/trusted/bin',
        'PYTHONPATH': '/trusted/python',
        'ROS_DISTRO': 'humble',
        'HOME': '/home/operator',
        'ROS_HOME': str(private_home / 'ros'),
        'XDG_CACHE_HOME': str(private_home / 'cache'),
        'XDG_CONFIG_HOME': str(private_home / 'config'),
        'ROS_DOMAIN_ID': '73',
        'ROS_LOCALHOST_ONLY': '1',
        'ROS2CLI_NO_DAEMON': '1',
        'PYTHONDONTWRITEBYTECODE': '1',
    }
    assert gui['DISPLAY'] == ':99'
    assert gui['XAUTHORITY'] == '/private/xauth'
    rendered = json.dumps(gui, sort_keys=True)
    for secret in (
        'private-openai-secret',
        'private-agent-secret',
        'private-aws-secret',
        '/private/injected.so',
    ):
        assert secret not in rendered


@pytest.mark.parametrize('domain_id', [0, 101, -1, True, '73'])
def test_sanitized_environment_rejects_non_isolated_domain(
    tmp_path,
    domain_id,
) -> None:
    with pytest.raises(ValueError, match='domain ID'):
        sanitized_ros_environment(
            {},
            private_home=(tmp_path / 'private').resolve(),
            domain_id=domain_id,
            gui=False,
        )


def test_private_runtime_values_never_appear_in_repr(tmp_path) -> None:
    token = 'private-agent-token'
    user = 'private-user'
    nonce = '0123456789abcdef0123456789abcdef'
    database = (tmp_path / 'private-ledger.sqlite3').resolve()
    client = TextAgentHTTPClient(
        43871,
        token=token,
        user_id=user,
        run_nonce=nonce,
    )
    observer = SQLiteAcceptanceObserver(database)
    receipt = ProposalReceipt(_CONFIRMATION_ID)

    rendered = ' '.join((repr(client), repr(observer), repr(receipt)))
    for private in (
        token,
        user,
        nonce,
        str(database),
        _CONFIRMATION_ID,
        '거실로 가줘',
        '43871',
    ):
        assert private not in rendered
