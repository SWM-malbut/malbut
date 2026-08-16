"""Honest, non-physical Agent admission for Gazebo room monitoring."""

import copy
import sqlite3
from types import SimpleNamespace

import pytest

import malbut_agent_server.orchestrator as orchestrator_module
import malbut_agent_server.conversation as conversation_module
import test_monitor_room_simulation_execution as simulation_tests
from malbut_agent_server.confirmation import (
    build_monitor_room_confirmation,
)
from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.gateway import production_registry, simulation_registry
from malbut_agent_server.gazebo_execution_outbox import (
    GazeboSimulationExecutionPolicy,
)
from malbut_agent_server.homecam_semantic import (
    AuthenticatedHomecamSemanticResolver,
)
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.monitor_room_target import (
    gazebo_simulation_navigation_effects,
    resolve_monitor_room_target,
)
from malbut_agent_server.orchestrator import (
    AgentOrchestrator,
    GazeboSimulationEvidenceBinding,
)
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.robot_state import (
    GazeboSimulationAdmissionEvidence,
    ServerGazeboSimulationAdmissionSource,
    TrustedRobotStateError,
    parse_gazebo_simulation_state_envelope,
    parse_trusted_robot_state_envelope,
)
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import AgentRequest
from test_homecam_semantic import _Transport, _config
from test_robot_state import _BOOT_ID, _NONCE_A, _NOW_NS, _envelope


class _BootClock:
    def __init__(self, now_ns: int = _NOW_NS) -> None:
        self.now_ns = now_ns

    def __call__(self) -> int:
        return self.now_ns


class _WallClock:
    def __init__(self, now: float = 1002.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _SemanticSource:
    def __init__(self, evidence) -> None:
        self.evidence = evidence
        self.calls = 0

    def fetch_snapshot_evidence(self):
        self.calls += 1
        return self.evidence.canonical_copy()


class _SimulationStateSource:
    def __init__(self, evidence) -> None:
        self.evidence = evidence
        self.calls = 0

    def read(self):
        self.calls += 1
        return self.evidence


def _semantic_evidence():
    return AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=_Transport(),
        clock=lambda: 1002.0,
    ).fetch_snapshot_evidence()


def _simulation_envelope(target=None, **state_updates):
    value = _envelope()
    value['source']['physical_authority'] = False
    if target is not None:
        value['binding']['device_id'] = target.device_id
        value['binding']['map_id'] = target.map_id
        value['binding']['map_revision'] = target.map_revision
    unknown = (
        'battery_percent',
        'emergency_stop',
        'camera_available',
        'privacy_mode',
        'docked',
        'forbidden_zones',
    )
    for name in unknown:
        value['state'][name] = None
        value['evidence'][name] = None
    for name, item in state_updates.items():
        value['state'][name] = item
        value['evidence'][name] = (
            None
            if item is None
            else {
                'source': f'collector/{name}',
                'received_boottime_ns': str(_NOW_NS - 2_000_000),
            }
        )
    return value


def _simulation_evidence(target):
    return parse_gazebo_simulation_state_envelope(
        _simulation_envelope(target),
        expected_nonce=_NONCE_A,
        expected_device_id=target.device_id,
        expected_host_boot_id=_BOOT_ID,
        now_boottime_ns=_NOW_NS,
    )


def _admission_source(*, user_id='local-user'):
    semantic = _semantic_evidence()
    target = resolve_monitor_room_target(
        semantic.snapshot,
        '거실',
        gazebo_simulation_navigation_effects(),
    )
    boot = _BootClock()
    wall = _WallClock()
    semantic_source = _SemanticSource(semantic)
    state_source = _SimulationStateSource(
        _simulation_evidence(target)
    )
    source = ServerGazeboSimulationAdmissionSource._for_test(
        expected_user_id=user_id,
        expected_device_id=target.device_id,
        expected_map_id=target.map_id,
        expected_map_revision=target.map_revision,
        semantic_evidence_source=semantic_source,
        simulation_state_source=state_source,
        expected_host_boot_id=_BOOT_ID,
        boottime_ns=boot,
        wall_clock=wall,
    )
    return source, target, boot, wall, semantic_source, state_source


def test_simulation_parser_is_parallel_and_never_fakes_physical_facts():
    """The physical and non-physical wire profiles cannot cross-admit."""
    semantic = _semantic_evidence()
    target = resolve_monitor_room_target(
        semantic.snapshot,
        '거실',
        gazebo_simulation_navigation_effects(),
    )
    value = _simulation_envelope(target)

    evidence = _simulation_evidence(target)

    assert evidence.physical_authority is False
    private = evidence.to_private_dict()
    assert private['state'] == {
        'battery_percent': None,
        'navigation_available': True,
        'localization_ok': True,
        'emergency_stop': None,
        'camera_available': None,
        'privacy_mode': None,
        'docked': None,
        'forbidden_zones': None,
    }
    with pytest.raises(TrustedRobotStateError) as physical:
        parse_trusted_robot_state_envelope(
            value,
            expected_nonce=_NONCE_A,
            expected_device_id=target.device_id,
            expected_host_boot_id=_BOOT_ID,
            now_boottime_ns=_NOW_NS,
        )
    assert physical.value.code == 'robot_state_physical_authority_missing'

    forged = copy.deepcopy(value)
    forged['state']['camera_available'] = False
    forged['evidence']['camera_available'] = {
        'source': 'collector/camera_available',
        'received_boottime_ns': str(_NOW_NS - 2_000_000),
    }
    with pytest.raises(TrustedRobotStateError) as simulation:
        parse_gazebo_simulation_state_envelope(
            forged,
            expected_nonce=_NONCE_A,
            expected_device_id=target.device_id,
            expected_host_boot_id=_BOOT_ID,
            now_boottime_ns=_NOW_NS,
        )
    assert simulation.value.code == (
        'robot_state_simulation_physical_fact_present'
    )


def test_server_admission_binds_principal_semantics_target_and_readiness():
    """One sealed issuance contains no physical or Homecam authority."""
    source, target, _boot, _wall, semantic, robot = _admission_source()

    evidence = source.issue(user_id='local-user', location='거실')

    assert type(evidence) is GazeboSimulationAdmissionEvidence
    assert evidence.matches_target(target)
    assert evidence.user_id == 'local-user'
    assert evidence.physical_authority is False
    assert evidence.physical_authorized is False
    assert evidence.physical_effects is False
    assert evidence.require_ready(_NOW_NS).navigation_available is True
    assert evidence.semantic_evidence.content_sha256 == (
        evidence.semantic_content_sha256
    )
    assert evidence.robot_state_evidence.physical_authority is False
    assert semantic.calls == robot.calls == 1

    with pytest.raises(TrustedRobotStateError) as raised:
        source.issue(user_id='other-user', location='거실')
    assert raised.value.code == 'robot_state_simulation_principal_mismatch'
    assert semantic.calls == robot.calls == 1


def test_orchestrator_persists_exact_simulation_binding_and_confirmation(
    tmp_path,
    monkeypatch,
):
    """Gazebo proposal reaches durable honest consent and exact replay."""
    source, target, boot, _wall, semantic, robot = _admission_source()
    monkeypatch.setattr(
        orchestrator_module,
        'trusted_boottime_ns',
        boot,
    )
    database = tmp_path / 'gazebo-agent-confirmation.sqlite3'
    conversations = SQLiteConversationStore(str(database))
    conversations.create('local-user', 'gazebo-conversation')
    orchestrator = AgentOrchestrator(
        provider=MockProvider(),
        memory_store=SQLiteMemoryStore(':memory:'),
        conversation_store=conversations,
        safety_policy=SafetyPolicy(monitorable_locations=['거실']),
        capability_registry=simulation_registry(),
        gazebo_simulation_admission_source=source,
    )
    request = AgentRequest.from_dict({
        'request_id': 'gazebo-agent-request',
        'user_id': 'local-user',
        'conversation_id': 'gazebo-conversation',
        'turn_id': 'gazebo-turn',
        'utterance': '거실 전체를 보여줘',
        # Deliberately hostile/untrusted physical state is ignored.
        'robot_state': {
            'navigation_available': False,
            'localization_ok': False,
            'emergency_stop': True,
            'privacy_mode': True,
            'camera_available': False,
        },
        'available_tools': ['monitor_room'],
    })

    result = orchestrator.handle(request)

    assert result.safety.allowed is True
    assert result.current_state_trusted() is True
    assert isinstance(
        result.state_evidence,
        GazeboSimulationEvidenceBinding,
    )
    assert result.state_evidence.user_id == 'local-user'
    assert result.state_evidence.target_binding_digest == (
        target.binding_digest
    )
    persisted = result.to_persisted_dict()
    assert persisted['schema_version'] == 4
    confirmation = build_monitor_room_confirmation(
        'local-user',
        'gazebo-speech-session',
        'gazebo-utterance',
        result,
        target,
    )
    assert 'Gazebo 시뮬레이션 로봇이 이동' in confirmation.message
    assert '기존 홈캠 스트리밍은 계속 실행' in confirmation.message
    assert '시작·중지·재설정하지 않습니다' in confirmation.message
    assert '실제 로봇 이동' in confirmation.message
    assert confirmation.target.effects.schema_version == 2
    stored = conversations.register_confirmation_intent(
        confirmation.to_intent_draft()
    )
    assert stored.reconstruct_target_binding().effects == target.effects

    replay = orchestrator.handle(request)
    assert replay.safety.allowed is True
    assert replay.current_state_trusted() is True
    assert replay.state_evidence == result.state_evidence
    assert semantic.calls == robot.calls == 2
    conversations.close()

    reopened = SQLiteConversationStore(str(database))
    try:
        restored = reopened.get_confirmation_intent(
            'local-user',
            confirmation.confirmation_request_id,
        )
        assert restored.confirmation_message == confirmation.message
        assert restored.reconstruct_target_binding().effects == target.effects
    finally:
        reopened.close()


def test_simulation_admission_cannot_be_composed_in_physical_runtime():
    """Default/physical registries cannot consume Gazebo admission."""
    source, _target, _boot, _wall, _semantic, _robot = _admission_source()
    with pytest.raises(ValueError, match='requires simulation'):
        AgentOrchestrator(
            provider=MockProvider(),
            memory_store=SQLiteMemoryStore(':memory:'),
            conversation_store=SQLiteConversationStore(':memory:'),
            safety_policy=SafetyPolicy(monitorable_locations=['거실']),
            capability_registry=production_registry(),
            gazebo_simulation_admission_source=source,
        )


def test_approved_simulation_profile_reaches_gazebo_outbox(
    tmp_path,
    monkeypatch,
):
    """A v2 approval can enqueue while all durable physical flags stay off."""
    source, target, boot, _wall, _semantic, _robot = _admission_source(
        user_id='simulation-user'
    )
    policy = GazeboSimulationExecutionPolicy._for_gazebo_admission_test(
        robot_id=target.device_id,
        expected_device_id=target.device_id,
        simulation_admission_source=source,
        expected_host_boot_id=_BOOT_ID,
        boottime_ns=boot,
    )
    monkeypatch.setattr(orchestrator_module, 'trusted_boottime_ns', boot)
    monkeypatch.setattr(
        orchestrator_module,
        'time',
        SimpleNamespace(time=lambda: 100.0),
    )
    wall = simulation_tests.MutableClock(100.0)
    store = SQLiteConversationStore(
        str(tmp_path / 'gazebo-v2-outbox.sqlite3'),
        clock=wall,
        simulation_execution_verifier=simulation_tests._TEST_TRUST,
        gazebo_execution_policy=policy,
    )
    try:
        store.create('simulation-user', 'gazebo-outbox-conversation')
        orchestrator = AgentOrchestrator(
            provider=MockProvider(),
            memory_store=SQLiteMemoryStore(':memory:'),
            conversation_store=store,
            safety_policy=SafetyPolicy(monitorable_locations=['거실']),
            capability_registry=simulation_registry(),
            gazebo_simulation_admission_source=source,
        )
        request = AgentRequest.from_dict({
            'request_id': 'gazebo-outbox-agent-request',
            'user_id': 'simulation-user',
            'conversation_id': 'gazebo-outbox-conversation',
            'turn_id': 'gazebo-outbox-turn',
            'utterance': '거실 전체를 보여줘',
            'robot_state': None,
            'available_tools': ['monitor_room'],
        })
        proposal = orchestrator.handle(request)
        confirmation = build_monitor_room_confirmation(
            'simulation-user',
            'gazebo-outbox-speech',
            'gazebo-outbox-utterance',
            proposal,
            target,
        )
        draft = confirmation.to_intent_draft()
        store.register_confirmation_intent(draft)
        scenario = simulation_tests._approve(
            store,
            wall,
            draft,
            target,
            consume_request_id='gazebo-v2-consume',
        )
        result = store.consume_approved_monitor_room_gazebo_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )
        row = store._connection.execute(
            'SELECT * FROM monitor_room_gazebo_execution_outbox'
        ).fetchone()
        assert result.enqueue is not None
        assert scenario.target.effects.gazebo_simulation_navigation
        assert row['runtime_mode'] == 'gazebo'
        assert row['simulation'] == 1
        assert row['physical_authorized'] == 0
        assert row['physical_effects'] == 0
        assert row['viewer_live'] == 0
        assert row['camera_coverage_validated'] == 0
    finally:
        store.close()


def test_confirmation_storage_v2_migrates_without_rewriting_legacy_effects(
    tmp_path,
):
    """The exact effects-v1 table widens to v2 without semantic drift."""
    database = tmp_path / 'confirmation-effects-v2-migration.sqlite3'
    wall = simulation_tests.MutableClock(100.0)
    store = SQLiteConversationStore(
        str(database),
        clock=wall,
        simulation_execution_verifier=simulation_tests._TEST_TRUST,
    )
    draft, target = simulation_tests._commit_confirmation(
        store,
        wall,
        suffix='storage-v2-legacy',
    )
    assert target.effects.schema_version == 1
    store.close()

    connection = sqlite3.connect(str(database))
    try:
        connection.execute('PRAGMA legacy_alter_table=ON')
        connection.execute('BEGIN IMMEDIATE')
        dependent_triggers = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND sql IS NOT NULL "
            "AND instr(lower(sql), 'confirmation_intents') > 0"
        ).fetchall()
        for name, _sql in dependent_triggers:
            connection.execute(
                f'DROP TRIGGER "{name.replace(chr(34), chr(34) * 2)}"'
            )
        connection.execute(
            'ALTER TABLE confirmation_intents '
            'RENAME TO confirmation_intents_current_backup'
        )
        connection.execute(
            conversation_module.CONFIRMATION_INTENTS_TABLE_STORAGE_V2_SQL
        )
        columns = [
            row[1]
            for row in connection.execute(
                'PRAGMA table_info(confirmation_intents)'
            ).fetchall()
        ]
        joined = ', '.join(columns)
        connection.execute(
            f'INSERT INTO confirmation_intents ({joined}) '
            f'SELECT {joined} FROM confirmation_intents_current_backup'
        )
        connection.execute(
            'DROP TABLE confirmation_intents_current_backup'
        )
        connection.execute(
            conversation_module.CONFIRMATION_RESPONSE_OWNER_INDEX_SQL
        )
        connection.execute(
            conversation_module.CONFIRMATION_ONE_PENDING_SESSION_INDEX_SQL
        )
        connection.execute('DROP TABLE confirmation_schema_metadata')
        connection.execute(
            conversation_module
            .CONFIRMATION_SCHEMA_METADATA_STORAGE_V2_TABLE_SQL
        )
        connection.execute(
            'INSERT INTO confirmation_schema_metadata '
            '(singleton, schema_version) VALUES (1, 2)'
        )
        for _name, trigger_sql in dependent_triggers:
            connection.execute(trigger_sql)
        connection.commit()
    finally:
        connection.close()

    migrated = SQLiteConversationStore(str(database), clock=wall)
    try:
        metadata = migrated._connection.execute(
            'SELECT schema_version FROM confirmation_schema_metadata'
        ).fetchone()
        restored = migrated.get_confirmation_intent(
            draft.user_id,
            draft.confirmation_request_id,
        )
        assert metadata[0] == 3
        assert restored.state == 'pending'
        assert restored.reconstruct_target_binding() == target
        assert restored.reconstruct_target_binding().effects.schema_version == 1
    finally:
        migrated.close()
