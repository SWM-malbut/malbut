"""Production composition for explicit Agent-to-Gazebo preparation."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

import pytest

from malbut_agent_server import factory as factory_module
from malbut_agent_server.cli import server_main
from malbut_agent_server.config import Settings
from malbut_agent_server.factory import (
    build_monitor_room_target_resolver,
    build_orchestrator,
    get_gazebo_simulation_execution_seam,
)
from malbut_agent_server.gazebo_simulation_execution import (
    GazeboSimulationExecutionError,
    GazeboSimulationExecutionSeam,
)
from malbut_agent_server.gazebo_simulation_authority import (
    ServerGazeboSimulationApprovalConsumer,
)
from malbut_agent_server.gazebo_monitor_room_command_runner import (
    GazeboMonitorRoomCommandRunner,
)
from malbut_agent_server.gazebo_monitor_room_gateway_client import (
    GazeboMonitorRoomGatewayClient,
)
from malbut_agent_server.gazebo_prepare_dispatcher import GazeboPrepareClient
from malbut_agent_server.homecam_semantic import HTTPSHomecamSemanticTransport
from malbut_agent_server.http_server import make_server
from malbut_agent_server.monitor_room_target import (
    resolve_monitor_room_target,
)
from malbut_agent_server.robot_state import UnixSocketTrustedRobotStateSource
from malbut_gazebo.gazebo_monitor_room_prepare_gateway import (
    GazeboMonitorRoomPrepareProcessor,
    GazeboMonitorRoomPrepareServer,
)
from malbut_gazebo.gazebo_monitor_room_gateway import (
    GazeboMonitorRoomGatewayProcessor,
    GazeboMonitorRoomGatewayReplayStore,
    GazeboMonitorRoomGatewayServer,
)
from malbut_gazebo.gazebo_monitor_room_nav2_adapter import (
    GazeboMonitorRoomNav2Controller,
)
from malbut_gazebo.gazebo_monitor_room_store import (
    GazeboMonitorRoomStore,
)
import malbut_agent_server.gazebo_execution_outbox as outbox_module
import test_gazebo_simulation_authority as authority_tests
import test_gazebo_monitor_room_command_runner as runner_tests
import test_homecam_semantic as semantic_tests
import test_monitor_room_simulation_execution as simulation_tests
from test_robot_state import _BOOT_ID, _NOW_NS


_AUTH_TOKEN = 'production-gazebo-http-token-0123456789abcdef'
_AUTHORITY_SECRET = 'production-gazebo-authority-secret-' + 's' * 32
_USER_ID = 'simulation-user'


class MutableBoot:
    """Expose one production boot source whose identity can drift."""

    def __init__(self) -> None:
        """Start at the trusted test host and BOOTTIME."""
        self.boot_id = _BOOT_ID
        self.now_ns = _NOW_NS

    def read_boot_id(self) -> str:
        """Return the currently configured host identity."""
        return self.boot_id

    def read_boottime_ns(self) -> int:
        """Return the currently configured BOOTTIME sample."""
        return self.now_ns


class ProductionComposition:
    """Own one enabled runtime and its mutable trusted test sources."""

    def __init__(
        self,
        *,
        settings,
        orchestrator,
        target,
        semantic_transport,
        robot_source,
        boot,
    ) -> None:
        """Keep only values needed by focused integration assertions."""
        self.settings = settings
        self.orchestrator = orchestrator
        self.target = target
        self.semantic_transport = semantic_transport
        self.robot_source = robot_source
        self.boot = boot

    @property
    def seam(self):
        """Return the configured explicit execution seam."""
        return get_gazebo_simulation_execution_seam(self.orchestrator)

    def close(self) -> None:
        """Close the two SQLite services owned by the orchestrator."""
        self.orchestrator.conversation_store.close()
        self.orchestrator.memory_store.close()


def _current_envelope(*, semantics=None):
    now_ms = int(time.time() * 1000.0)
    value = semantic_tests._envelope_for_semantics(
        semantic_tests._room_payload() if semantics is None else semantics,
        issuedAtMs=now_ms - 100,
        expiresAtMs=now_ms + 9000,
        agentUserId=_USER_ID,
    )
    return semantic_tests._resign(value)


def _settings(tmp_path, *, database_name='agent.sqlite3') -> Settings:
    return Settings(
        provider='mock',
        tool_mode='simulation',
        database_path=str(tmp_path / database_name),
        user_id=_USER_ID,
        auth_token=_AUTH_TOKEN,
        homecam_origin='https://homecam.example.test',
        homecam_agent_token=semantic_tests._SERVICE_TOKEN,
        homecam_signing_secret=semantic_tests._SIGNING_SECRET,
        homecam_principal_subject_digest=semantic_tests._SUBJECT_DIGEST,
        homecam_device_id='malbut-sim-01',
        robot_state_socket_path=str(tmp_path / 'robot-state.sock'),
        robot_state_expected_uid=os.geteuid(),
        robot_state_device_id='malbut-sim-01',
        monitorable_rooms=('거실',),
        enable_gazebo_simulation_execution=True,
        gazebo_simulation_authority_secret=_AUTHORITY_SECRET,
        gazebo_prepare_socket_path=str(tmp_path / 'prepare.sock'),
        gazebo_prepare_expected_uid=os.geteuid(),
        gazebo_prepare_timeout_seconds=2,
        gazebo_prepare_lease_seconds=30,
    )


def _build_composition(
    tmp_path,
    monkeypatch,
    *,
    database_name='agent.sqlite3',
) -> ProductionComposition:
    settings = _settings(tmp_path, database_name=database_name)
    envelope = _current_envelope()
    baseline_transport = semantic_tests._Transport(envelope)
    baseline_resolver = build_monitor_room_target_resolver(
        settings,
        transport=baseline_transport,
    )
    assert baseline_resolver is not None
    evidence = baseline_resolver.fetch_snapshot_evidence()
    target = resolve_monitor_room_target(
        evidence.snapshot,
        '거실',
        simulation_tests._target(
            '{"location":"거실"}',
            'production-effects',
        ).effects,
    )
    semantic_transport = semantic_tests._Transport(envelope)
    resolver = build_monitor_room_target_resolver(
        settings,
        transport=semantic_transport,
    )
    assert resolver is not None
    robot_source = authority_tests.StaticRobotStateSource(
        authority_tests._robot_evidence(target)
    )
    boot = MutableBoot()
    monkeypatch.setattr(
        outbox_module,
        '_read_local_boot_id',
        boot.read_boot_id,
    )
    monkeypatch.setattr(
        outbox_module,
        'trusted_boottime_ns',
        boot.read_boottime_ns,
    )
    orchestrator = build_orchestrator(
        settings,
        trusted_robot_state_source=robot_source,
        monitor_room_target_resolver=resolver,
    )
    assert semantic_transport.calls == []
    assert robot_source.calls == 0
    return ProductionComposition(
        settings=settings,
        orchestrator=orchestrator,
        target=target,
        semantic_transport=semantic_transport,
        robot_source=robot_source,
        boot=boot,
    )


def _resolve_approval(composition, monkeypatch, *, suffix):
    monkeypatch.setattr(
        simulation_tests,
        '_target',
        lambda _arguments, _suffix: composition.target,
    )
    wall = simulation_tests.MutableClock(time.time())
    draft, _, _ = authority_tests._resolve(
        composition.orchestrator.conversation_store,
        wall,
        suffix=suffix,
    )
    return draft


def _post(url: str, payload: dict, *, token=''):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers=headers,
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _http_server(composition):
    server = make_server(
        '127.0.0.1',
        0,
        composition.orchestrator,
        auth_token=_AUTH_TOKEN,
        allowed_user_id=_USER_ID,
        gazebo_simulation_execution_seam=composition.seam,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f'http://{host}:{port}'


def _prepare_server(tmp_path, composition):
    operations = GazeboMonitorRoomStore(
        tmp_path / 'gazebo-operations.sqlite3',
        boot_id_reader=composition.boot.read_boot_id,
    )
    processor = GazeboMonitorRoomPrepareProcessor(
        operations,
        expected_robot_id=composition.target.device_id,
        local_boot_id=_BOOT_ID,
        clock=lambda: composition.boot.now_ns / 1_000_000_000,
    )
    server = GazeboMonitorRoomPrepareServer(
        processor,
        composition.settings.gazebo_prepare_socket_path,
        expected_agent_uid=os.geteuid(),
    )
    server.start()
    return operations, server


def test_disabled_default_constructs_no_execution_collaborator(
    monkeypatch,
) -> None:
    """Default startup has no authority, policy, client, or dispatcher."""
    calls = []

    def unexpected(*_args, **_kwargs):
        calls.append('called')
        raise AssertionError('disabled execution constructed a collaborator')

    for name in (
        'ServerGazeboSimulationExecutionVerifier',
        'GazeboSimulationExecutionPolicy',
        'ServerGazeboSimulationApprovalConsumer',
        'GazeboPrepareClient',
        'GazeboPrepareDispatcher',
        'GazeboSimulationExecutionSeam',
    ):
        monkeypatch.setattr(factory_module, name, unexpected)
    runtime = build_orchestrator(Settings(database_path=':memory:'))
    try:
        assert get_gazebo_simulation_execution_seam(runtime) is None
        assert calls == []
    finally:
        runtime.conversation_store.close()
        runtime.memory_store.close()


def test_cli_check_composes_enabled_runtime_without_external_calls(
    tmp_path,
    monkeypatch,
) -> None:
    """Production entrypoint validates the full seam while remaining inert."""
    values = {
        'MALBUT_AGENT_PROVIDER': 'mock',
        'MALBUT_AGENT_TOOL_MODE': 'simulation',
        'MALBUT_AGENT_USER_ID': _USER_ID,
        'MALBUT_AGENT_AUTH_TOKEN': _AUTH_TOKEN,
        'MALBUT_HOMECAM_ORIGIN': 'https://homecam.example.test',
        'MALBUT_HOMECAM_AGENT_TOKEN': semantic_tests._SERVICE_TOKEN,
        'MALBUT_HOMECAM_SIGNING_SECRET': semantic_tests._SIGNING_SECRET,
        'MALBUT_HOMECAM_PRINCIPAL_SUBJECT_DIGEST': (
            semantic_tests._SUBJECT_DIGEST
        ),
        'MALBUT_HOMECAM_DEVICE_ID': 'malbut-sim-01',
        'MALBUT_ROBOT_STATE_SOCKET_PATH': str(tmp_path / 'state.sock'),
        'MALBUT_ROBOT_STATE_EXPECTED_UID': str(os.geteuid()),
        'MALBUT_ROBOT_STATE_DEVICE_ID': 'malbut-sim-01',
        'MALBUT_AGENT_MONITORABLE_ROOMS': '거실',
        'MALBUT_AGENT_ENABLE_GAZEBO_SIMULATION_EXECUTION': 'true',
        'MALBUT_AGENT_GAZEBO_SIMULATION_AUTHORITY_SECRET': (
            _AUTHORITY_SECRET
        ),
        'MALBUT_AGENT_GAZEBO_PREPARE_SOCKET_PATH': str(
            tmp_path / 'prepare.sock'
        ),
        'MALBUT_AGENT_GAZEBO_PREPARE_EXPECTED_UID': str(os.geteuid()),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    def unexpected(*_args, **_kwargs):
        raise AssertionError('configuration check performed external I/O')

    monkeypatch.setattr(HTTPSHomecamSemanticTransport, 'fetch', unexpected)
    monkeypatch.setattr(UnixSocketTrustedRobotStateSource, 'read', unexpected)
    monkeypatch.setattr(GazeboPrepareClient, 'prepare', unexpected)

    assert server_main(
        [
            '--env-file',
            str(tmp_path / 'missing.env'),
            '--database',
            str(tmp_path / 'cli.sqlite3'),
            '--check',
        ]
    ) == 0


def test_execution_configuration_is_all_or_nothing_and_redacted(
    tmp_path,
) -> None:
    """Missing or dormant authority bindings fail before composition."""
    defaults = Settings.from_env({})
    assert defaults.enable_gazebo_simulation_execution is False
    assert defaults.gazebo_simulation_authority_secret == ''
    assert defaults.gazebo_prepare_socket_path == ''
    assert defaults.gazebo_prepare_expected_uid is None

    with pytest.raises(ValueError, match='explicit enablement'):
        replace(
            defaults,
            gazebo_simulation_authority_secret='a' * 64,
        ).validate_for_server()

    valid = _settings(tmp_path)
    valid.validate_for_server()
    cases = (
        (
            {'tool_mode': 'proposal'},
            'TOOL_MODE=simulation',
        ),
        (
            {'database_path': ':memory:'},
            'file-backed',
        ),
        (
            {'auth_token': 'short'},
            'strong',
        ),
        (
            {'gazebo_simulation_authority_secret': ''},
            'AUTHORITY_SECRET',
        ),
        (
            {'gazebo_prepare_socket_path': ''},
            'SOCKET_PATH',
        ),
        (
            {'gazebo_prepare_expected_uid': None},
            'EXPECTED_UID',
        ),
        (
            {'monitorable_rooms': ()},
            'monitorable room',
        ),
        (
            {'robot_state_device_id': 'other-device'},
            'device IDs must match',
        ),
    )
    for updates, message in cases:
        with pytest.raises(ValueError, match=message):
            replace(valid, **updates).validate_for_server()

    rendered = repr(valid)
    for private in (
        _AUTH_TOKEN,
        _AUTHORITY_SECRET,
        valid.gazebo_prepare_socket_path,
        semantic_tests._SERVICE_TOKEN,
        semantic_tests._SIGNING_SECRET,
    ):
        assert private not in rendered
    assert 'gazebo_simulation_authority_secret=<redacted>' in rendered
    assert 'gazebo_prepare_socket_path=<redacted>' in rendered


def test_authenticated_http_approval_enqueues_and_prepares_then_replays(
    tmp_path,
    monkeypatch,
) -> None:
    """One approved confirmation reaches the real protected prepare UDS."""
    composition = _build_composition(tmp_path, monkeypatch)
    draft = _resolve_approval(
        composition,
        monkeypatch,
        suffix='production-http',
    )
    operations, prepare = _prepare_server(tmp_path, composition)
    http, http_thread, origin = _http_server(composition)
    endpoint = (
        origin
        + '/v1/internal/gazebo-simulation/consume-and-prepare'
    )
    payload = {'confirmation_request_id': draft.confirmation_request_id}
    try:
        unauthorized, unauthorized_body = _post(endpoint, payload)
        assert unauthorized == 401
        assert unauthorized_body['error']['code'] == 'unauthorized'

        prepare_thread = threading.Thread(target=prepare.serve_once)
        prepare_thread.start()
        status, body = _post(endpoint, payload, token=_AUTH_TOKEN)
        prepare_thread.join(timeout=5)
        assert not prepare_thread.is_alive()

        assert status == 200
        assert body['simulation'] is True
        assert body['physical_authorized'] is False
        assert body['physical_effects'] is False
        assert body['viewer_live'] is False
        assert body['prepared'] is True
        assert 'prepared_authority' not in body
        assert body['preparation']['state'] == 'prepared'
        assert body['preparation']['prepare_replayed'] is False
        assert body['consume']['gazebo_execution']['state'] == 'pending'
        assert operations._connection.execute(
            'SELECT COUNT(*) FROM gazebo_monitor_room_operations'
        ).fetchone()[0] == 1
        assert composition.semantic_transport.calls != []
        assert composition.robot_source.calls == 1

        replay_status, replay = _post(
            endpoint,
            payload,
            token=_AUTH_TOKEN,
        )
        assert replay_status == 200
        assert replay['prepared'] is True
        assert 'prepared_authority' not in replay
        assert replay['consume']['simulation_receipt']['replayed'] is True
        assert replay['consume']['gazebo_execution']['state'] == 'prepared'
        assert replay['preparation'] is None
        rendered = json.dumps(body, ensure_ascii=False, sort_keys=True)
        for private in (
            _AUTHORITY_SECRET,
            composition.settings.gazebo_prepare_socket_path,
            composition.target.device_id,
            composition.target.map_id,
            _BOOT_ID,
            'claim_token',
            'prepare_fingerprint',
            'x_mm',
            'y_mm',
        ):
            assert private not in rendered
    finally:
        http.shutdown()
        http.server_close()
        http_thread.join(timeout=2)
        prepare.close()
        operations.close()
        composition.close()

    restarted = _build_composition(
        tmp_path,
        monkeypatch,
        database_name='agent.sqlite3',
    )
    try:
        result = GazeboSimulationExecutionSeam.consume_and_prepare(
            restarted.seam,
            draft.confirmation_request_id,
        )
        public = result.to_public_dict()
        assert public['consume']['simulation_receipt']['replayed'] is True
        assert public['consume']['gazebo_execution']['state'] == 'prepared'
        assert public['preparation'] is None
        assert public['prepared'] is True
        assert result.prepared_authority is not None
        assert result.prepared_authority.confirmation_request_id == (
            draft.confirmation_request_id
        )
        assert restarted.semantic_transport.calls == []
        assert restarted.robot_source.calls == 0

        authority = result.prepared_authority
        with pytest.raises(AttributeError):
            object.__setattr__(
                result,
                'prepared_authority',
                object(),
            )
        foreign_authority = replace(
            authority,
            confirmation_request_id=(
                'simulation-confirmation-foreign-authority'
            ),
        )
        object.__setattr__(
            result,
            '_prepared_authority',
            foreign_authority,
        )
        for render in (
            lambda: result.prepared,
            result.to_public_dict,
            lambda: repr(result),
            lambda: result.prepared_authority,
        ):
            with pytest.raises(GazeboSimulationExecutionError) as caught:
                render()
            assert caught.value.code == (
                'gazebo_simulation_result_invalid'
            )
        object.__setattr__(result, '_prepared_authority', authority)
        object.__setattr__(result.consume.receipt, 'state', 'forged')
        with pytest.raises(GazeboSimulationExecutionError) as caught:
            result.to_public_dict()
        assert caught.value.code == 'gazebo_simulation_result_invalid'
    finally:
        restarted.close()


def test_committed_ack_http_failure_restarts_with_internal_selector(
    tmp_path,
    monkeypatch,
) -> None:
    """A lost ACK return is confirmation-only drive-ready after restart."""
    composition = _build_composition(
        tmp_path,
        monkeypatch,
        database_name='ack-response-loss.sqlite3',
    )
    draft = _resolve_approval(
        composition,
        monkeypatch,
        suffix='production-ack-response-loss',
    )
    operations, prepare = _prepare_server(tmp_path, composition)
    http, http_thread, origin = _http_server(composition)
    endpoint = origin + (
        '/v1/internal/gazebo-simulation/consume-and-prepare'
    )
    payload = {'confirmation_request_id': draft.confirmation_request_id}
    store_type = type(composition.orchestrator.conversation_store)
    original_ack = store_type.acknowledge_gazebo_execution

    def lose_committed_ack_return(current, **values):
        original_ack(current, **values)
        raise OSError('private committed ACK response loss')

    monkeypatch.setattr(
        store_type,
        'acknowledge_gazebo_execution',
        lose_committed_ack_return,
    )
    try:
        prepare_thread = threading.Thread(target=prepare.serve_once)
        prepare_thread.start()
        failed_status, failed = _post(
            endpoint,
            payload,
            token=_AUTH_TOKEN,
        )
        prepare_thread.join(timeout=5)
        assert not prepare_thread.is_alive()
        assert failed_status == 503
        assert failed['error']['code'] == (
            'gazebo_simulation_prepare_unavailable'
        )
        assert operations._connection.execute(
            'SELECT COUNT(*) FROM gazebo_monitor_room_operations'
        ).fetchone()[0] == 1
        row = composition.orchestrator.conversation_store._connection.execute(
            'SELECT state FROM monitor_room_gazebo_execution_outbox '
            'WHERE confirmation_request_id = ?',
            (draft.confirmation_request_id,),
        ).fetchone()
        assert row['state'] == 'prepared'
    finally:
        monkeypatch.setattr(
            store_type,
            'acknowledge_gazebo_execution',
            original_ack,
        )
        http.shutdown()
        http.server_close()
        http_thread.join(timeout=2)
        prepare.close()
        composition.close()

    restarted = _build_composition(
        tmp_path,
        monkeypatch,
        database_name='ack-response-loss.sqlite3',
    )
    restarted_http, restarted_thread, restarted_origin = _http_server(
        restarted
    )
    try:
        replay_status, replay = _post(
            restarted_origin
            + '/v1/internal/gazebo-simulation/consume-and-prepare',
            payload,
            token=_AUTH_TOKEN,
        )
        assert replay_status == 200
        assert replay['prepared'] is True
        assert replay['preparation'] is None
        assert replay['consume']['gazebo_execution']['state'] == 'prepared'

        # This confirmation-only durable resolution is the authority source
        # used by the later explicit command runner; no caller-supplied
        # outbox, operation, fence, coordinate, or fingerprint is accepted.
        internal = restarted.seam.consume_and_prepare(
            draft.confirmation_request_id
        )
        authority = internal.prepared_authority
        assert internal.prepared is True
        assert internal.preparation is None
        assert authority is not None
        assert authority.confirmation_request_id == (
            draft.confirmation_request_id
        )
        assert authority.outbox_id == (
            internal.consume.enqueue.outbox_id
        )
        assert authority.operation_id == (
            internal.consume.enqueue.operation_id
        )
        assert restarted.semantic_transport.calls == []
        assert restarted.robot_source.calls == 0

        # Manually compose the separately bounded command runner and prove
        # that the same confirmation alone recovers the prepared selector
        # and reaches one real protected gateway command after restart.
        def command_clock():
            return restarted.boot.now_ns / 1_000_000_000

        port = runner_tests._Port()
        controller = GazeboMonitorRoomNav2Controller(
            operations,
            port,
            worker_id='production-restart-manual-runner',
            lease_seconds=20.0,
            clock=command_clock,
        )
        replay_store = GazeboMonitorRoomGatewayReplayStore(
            tmp_path / 'restart-command-replay.sqlite3',
            core_store_namespace=operations.store_namespace,
            clock=command_clock,
        )
        processor = GazeboMonitorRoomGatewayProcessor(
            operations,
            controller,
            replay_store,
        )
        command_path = tmp_path / 'restart-command.sock'
        command_server = GazeboMonitorRoomGatewayServer(
            processor,
            command_path,
            expected_agent_uid=os.geteuid(),
        )
        command_server.start()
        runner = GazeboMonitorRoomCommandRunner(
            restarted.orchestrator.conversation_store,
            GazeboMonitorRoomGatewayClient(
                str(command_path),
                expected_server_uid=os.geteuid(),
                timeout_seconds=2.0,
            ),
            user_id=_USER_ID,
        )
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                serving = pool.submit(
                    lambda: [
                        command_server.serve_once()
                        for _index in range(2)
                    ]
                )
                first_step = runner.drive_once(
                    draft.confirmation_request_id,
                    'production-restart-manual-drive',
                    timeout_seconds=2.0,
                )
                step = runner.drive_once(
                    draft.confirmation_request_id,
                    'production-restart-manual-drive',
                    previous=first_step,
                    timeout_seconds=2.0,
                )
                serving.result(timeout=5.0)
            assert first_step.state == 'preflighting'
            assert step.state == 'navigating'
            assert step.simulation is True
            assert step.physical_authorized is False
            assert len(port.preflights) == 1
            assert len(port.starts) == 1
        finally:
            command_server.close()
            replay_store.close()
    finally:
        restarted_http.shutdown()
        restarted_http.server_close()
        restarted_thread.join(timeout=2)
        restarted.close()
        operations.close()


def test_explicit_confirmation_prepares_only_its_target_with_backlog(
    tmp_path,
    monkeypatch,
) -> None:
    """Pending A cannot be substituted for an explicitly requested B."""
    composition = _build_composition(tmp_path, monkeypatch)
    draft_a = _resolve_approval(
        composition,
        monkeypatch,
        suffix='production-backlog-a',
    )
    consumer = object.__getattribute__(composition.seam, '_consumer')
    consumed_a = ServerGazeboSimulationApprovalConsumer.consume(
        consumer,
        draft_a.confirmation_request_id,
    )
    assert consumed_a.enqueue is not None
    draft_b = _resolve_approval(
        composition,
        monkeypatch,
        suffix='production-backlog-b',
    )
    operations, prepare = _prepare_server(tmp_path, composition)
    try:
        prepare_thread = threading.Thread(target=prepare.serve_once)
        prepare_thread.start()
        result = GazeboSimulationExecutionSeam.consume_and_prepare(
            composition.seam,
            draft_b.confirmation_request_id,
        )
        prepare_thread.join(timeout=5)
        assert not prepare_thread.is_alive()
        assert result.consume.enqueue is not None
        assert result.preparation is not None
        assert result.preparation.outbox_id == result.consume.enqueue.outbox_id
        assert result.preparation.operation_id == (
            result.consume.enqueue.operation_id
        )
        assert result.consume.enqueue.outbox_id != consumed_a.enqueue.outbox_id
        rows = composition.orchestrator.conversation_store._connection.execute(
            'SELECT outbox_id, state FROM '
            'monitor_room_gazebo_execution_outbox'
        ).fetchall()
        states = {row['outbox_id']: row['state'] for row in rows}
        assert states[consumed_a.enqueue.outbox_id] == 'pending'
        assert states[result.consume.enqueue.outbox_id] == 'prepared'
        operation = operations._connection.execute(
            'SELECT operation_id FROM gazebo_monitor_room_operations'
        ).fetchone()
        assert operation['operation_id'] == result.consume.enqueue.operation_id
    finally:
        prepare.close()
        operations.close()
        composition.close()


@pytest.mark.parametrize('drift', ('semantic_map', 'robot_device', 'boot'))
def test_current_semantic_device_and_boot_drift_fail_before_outbox(
    tmp_path,
    monkeypatch,
    drift,
) -> None:
    """Every production binding is freshly checked before enqueue."""
    composition = _build_composition(
        tmp_path,
        monkeypatch,
        database_name=f'{drift}.sqlite3',
    )
    draft = _resolve_approval(
        composition,
        monkeypatch,
        suffix=f'production-{drift}',
    )
    if drift == 'semantic_map':
        semantics = semantic_tests._room_payload()
        semantics['mapRevision'] = 'grid-revision-changed'
        semantics['userMap']['map_revision'] = 'grid-revision-changed'
        composition.semantic_transport.envelope = _current_envelope(
            semantics=semantics
        )
    elif drift == 'robot_device':
        composition.robot_source.evidence = replace(
            composition.robot_source.evidence,
            device_id='other-device',
        )
    else:
        composition.boot.boot_id = (
            'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        )
    try:
        with pytest.raises(GazeboSimulationExecutionError) as caught:
            GazeboSimulationExecutionSeam.consume_and_prepare(
                composition.seam,
                draft.confirmation_request_id,
            )
        assert caught.value.code == 'gazebo_simulation_not_authorized'
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        store = composition.orchestrator.conversation_store
        assert store._connection.execute(
            'SELECT COUNT(*) '
            'FROM monitor_room_gazebo_execution_outbox'
        ).fetchone()[0] == 0
    finally:
        composition.close()


def test_wrong_http_principal_and_mutated_seam_fail_closed(
    tmp_path,
    monkeypatch,
) -> None:
    """HTTP identity and externally sealed composition cannot be swapped."""
    composition = _build_composition(tmp_path, monkeypatch)
    try:
        with pytest.raises(ValueError, match='principal and store'):
            make_server(
                '127.0.0.1',
                0,
                composition.orchestrator,
                auth_token=_AUTH_TOKEN,
                allowed_user_id='different-user',
                gazebo_simulation_execution_seam=composition.seam,
            )
        with pytest.raises(ValueError, match='bearer auth'):
            make_server(
                '127.0.0.1',
                0,
                composition.orchestrator,
                allowed_user_id=_USER_ID,
                gazebo_simulation_execution_seam=composition.seam,
            )
        object.__setattr__(
            composition.seam,
            '_user_id',
            'different-user',
        )
        with pytest.raises(GazeboSimulationExecutionError) as caught:
            composition.seam.consume_and_prepare(
                'simulation-confirmation-does-not-matter'
            )
        assert caught.value.code == (
            'gazebo_simulation_configuration_changed'
        )
    finally:
        composition.close()


def test_factory_and_http_reject_cross_store_seam_swap_and_route_hide(
    tmp_path,
    monkeypatch,
) -> None:
    """External seals reject a same-user foreign seam after composition."""
    primary = _build_composition(
        tmp_path,
        monkeypatch,
        database_name='sealed-primary.sqlite3',
    )
    foreign = _build_composition(
        tmp_path,
        monkeypatch,
        database_name='sealed-foreign.sqlite3',
    )
    original = primary.seam
    try:
        object.__setattr__(
            primary.orchestrator,
            'gazebo_simulation_execution_seam',
            foreign.seam,
        )
        with pytest.raises(GazeboSimulationExecutionError) as caught:
            get_gazebo_simulation_execution_seam(primary.orchestrator)
        assert caught.value.code == (
            'gazebo_simulation_configuration_changed'
        )
        object.__setattr__(
            primary.orchestrator,
            'gazebo_simulation_execution_seam',
            original,
        )

        http, thread, origin = _http_server(primary)
        endpoint = origin + (
            '/v1/internal/gazebo-simulation/consume-and-prepare'
        )
        try:
            for replacement in (foreign.seam, None):
                object.__setattr__(
                    http,
                    'gazebo_simulation_execution_seam',
                    replacement,
                )
                status, body = _post(
                    endpoint,
                    {
                        'confirmation_request_id': (
                            'simulation-confirmation-never-consume'
                        )
                    },
                    token=_AUTH_TOKEN,
                )
                assert status == 503
                assert body['error']['code'] == (
                    'gazebo_simulation_configuration_changed'
                )
            assert primary.semantic_transport.calls == []
            assert primary.robot_source.calls == 0
            assert foreign.semantic_transport.calls == []
            assert foreign.robot_source.calls == 0
        finally:
            http.shutdown()
            http.server_close()
            thread.join(timeout=2)
    finally:
        primary.close()
        foreign.close()


def test_agent_execution_modules_do_not_import_ros_or_gazebo_packages(
) -> None:
    """The Agent bridge remains transport-only and ROS-independent."""
    module_paths = (
        'gazebo_simulation_authority.py',
        'gazebo_simulation_execution.py',
        'gazebo_prepare_dispatcher.py',
    )
    source_root = Path(factory_module.__file__).parent
    for name in module_paths:
        source = (source_root / name).read_text(encoding='utf-8')
        assert 'import rclpy' not in source
        assert 'from rclpy' not in source
        assert 'from malbut_gazebo' not in source
        assert 'import malbut_gazebo' not in source
