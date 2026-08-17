"""Production server authority for approved Gazebo simulations."""

import hashlib
import json

import pytest

import test_monitor_room_simulation_execution as simulation_tests
from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.execution_ledger import (
    SimulationAssuranceError,
    SimulationConsumeRequest,
    VerifiedSimulationApproval,
)
from malbut_agent_server.gazebo_execution_outbox import (
    GazeboSimulationExecutionPolicy,
)
from malbut_agent_server.gazebo_simulation_authority import (
    ServerGazeboSimulationApprovalConsumer,
    ServerGazeboSimulationExecutionVerifier,
)
from malbut_agent_server.homecam_semantic import (
    AuthenticatedHomecamSemanticResolver,
)
from malbut_agent_server.monitor_room_target import (
    resolve_monitor_room_target,
)
from malbut_agent_server.robot_state import (
    parse_trusted_robot_state_envelope,
)
from test_homecam_semantic import (
    _Transport,
    _config,
    _envelope_for_semantics,
    _room_payload,
    _zones_payload,
)
from test_robot_state import _BOOT_ID, _NONCE_A, _NOW_NS, _envelope


_CAPABILITY = hashlib.sha256(
    b'malbut-production-server-test-process-capability'
).digest()
_USER_ID = 'simulation-user'


class BootClock:
    """Return one deterministic trusted BOOTTIME sample."""

    def __init__(self, now_ns: int = _NOW_NS) -> None:
        """Keep one exact test sample."""
        self.now_ns = now_ns

    def __call__(self) -> int:
        """Return the current test BOOTTIME."""
        return self.now_ns


class StaticSemanticSource:
    """Return detached copies of resolver-issued semantic evidence."""

    def __init__(self, evidence) -> None:
        """Keep one authenticated evidence baseline without fetching."""
        self.evidence = evidence
        self.calls = 0

    def fetch_snapshot_evidence(self):
        """Return one canonical detached evidence copy."""
        self.calls += 1
        return self.evidence.canonical_copy()


class StaticRobotStateSource:
    """Return one exact trusted robot-state envelope projection."""

    def __init__(self, evidence) -> None:
        """Keep one trusted robot-state baseline."""
        self.evidence = evidence
        self.calls = 0

    def read(self):
        """Return the configured robot-state evidence."""
        self.calls += 1
        return self.evidence


def _semantic_evidence(*, semantics=None):
    transport = _Transport(
        None
        if semantics is None
        else _envelope_for_semantics(semantics)
    )
    return AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=transport,
        clock=lambda: 1002.0,
    ).fetch_snapshot_evidence()


def _robot_evidence(target):
    value = _envelope(valid_until_ns=_NOW_NS + 1_000_000_000)
    value['binding']['map_id'] = target.map_id
    value['binding']['map_revision'] = target.map_revision
    return parse_trusted_robot_state_envelope(
        value,
        expected_nonce=_NONCE_A,
        expected_device_id=target.device_id,
        expected_host_boot_id=_BOOT_ID,
        now_boottime_ns=_NOW_NS,
    )


def _components(
    database,
    monkeypatch,
    *,
    semantic_source=None,
    policy_semantic_source=None,
):
    clock = simulation_tests.MutableClock(1002.0)
    evidence = _semantic_evidence()
    target = resolve_monitor_room_target(
        evidence.snapshot,
        '거실',
        simulation_tests._target(
            '{"location":"거실"}',
            'authority-effects',
        ).effects,
    )
    authority_source = (
        StaticSemanticSource(evidence)
        if semantic_source is None
        else semantic_source
    )
    policy_source = (
        StaticSemanticSource(evidence)
        if policy_semantic_source is None
        else policy_semantic_source
    )
    robot_source = StaticRobotStateSource(_robot_evidence(target))
    boot_clock = BootClock()
    verifier = ServerGazeboSimulationExecutionVerifier(
        _CAPABILITY,
        user_id=_USER_ID,
        semantic_evidence_source=authority_source,
        clock=clock,
    )
    policy = GazeboSimulationExecutionPolicy._for_test(
        robot_id=target.device_id,
        expected_device_id=target.device_id,
        semantic_evidence_source=policy_source,
        robot_state_source=robot_source,
        expected_host_boot_id=_BOOT_ID,
        boottime_ns=boot_clock,
    )
    monkeypatch.setattr(
        simulation_tests,
        '_target',
        lambda _arguments, _suffix: target,
    )
    store = SQLiteConversationStore(
        str(database),
        clock=clock,
        simulation_execution_verifier=verifier,
        gazebo_execution_policy=policy,
    )
    consumer = ServerGazeboSimulationApprovalConsumer(
        store,
        verifier,
        user_id=_USER_ID,
        semantic_evidence_source=authority_source,
    )
    return (
        store,
        consumer,
        verifier,
        clock,
        target,
        authority_source,
        policy_source,
        robot_source,
    )


def _resolve(store, clock, *, suffix='authority', disposition='approve'):
    draft, target = simulation_tests._commit_confirmation(
        store,
        clock,
        suffix=suffix,
    )
    terminal = store.resolve_confirmation_intent(
        user_id=draft.user_id,
        confirmation_request_id=draft.confirmation_request_id,
        proposal_fingerprint=draft.proposal_fingerprint,
        response_id=f'authority-response-{suffix}',
        response_fingerprint=simulation_tests._digest(
            f'authority-response-{suffix}'
        ),
        requested_disposition=disposition,
        response_channel='ui_in_process',
        assurance_level='unverified_in_process_ui',
        provenance_ref=simulation_tests._digest(
            f'authority-provenance-{suffix}'
        ),
    )
    return draft, target, terminal


def test_approved_intent_atomically_enqueues_without_physical_claims(
    tmp_path,
    monkeypatch,
) -> None:
    """Fresh signed semantics produce one simulation-only outbox row."""
    values = _components(tmp_path / 'authority.sqlite3', monkeypatch)
    store, consumer, _, clock, _, semantic, policy_semantic, robot = values
    draft, _, _ = _resolve(store, clock)

    result = consumer.consume(draft.confirmation_request_id)

    assert result.receipt.record_kind == 'planned'
    assert result.enqueue is not None
    assert result.enqueue.state == 'pending'
    assert semantic.calls == 2
    assert policy_semantic.calls == robot.calls == 1
    public = result.to_public_dict()
    execution = public['gazebo_execution']
    assert execution['simulation'] is True
    assert execution['physical_authorized'] is False
    assert execution['physical_effects'] is False
    assert execution['viewer_live'] is False
    assert 'x_mm' not in json.dumps(public, sort_keys=True)
    assert store._connection.execute(
        'SELECT COUNT(*) FROM monitor_room_gazebo_execution_outbox'
    ).fetchone()[0] == 1
    store.close()


def test_restart_reconstructs_identical_proof_and_replays_exact_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    """The same process capability survives a server/store reconstruction."""
    database = tmp_path / 'authority-restart.sqlite3'
    first = _components(database, monkeypatch)
    store, consumer, _, clock, _, _, _, _ = first
    draft, _, _ = _resolve(store, clock, suffix='restart')
    original = consumer.consume(draft.confirmation_request_id)
    store.close()

    second = _components(database, monkeypatch)
    reopened, replay_consumer, verifier, _, _, source, policy_source, robot = (
        second
    )
    replay = replay_consumer.consume(draft.confirmation_request_id)

    assert type(verifier) is ServerGazeboSimulationExecutionVerifier
    assert replay.receipt.replayed is True
    assert replay.receipt.receipt_digest == original.receipt.receipt_digest
    assert replay.enqueue is not None
    assert replay.enqueue.replayed is True
    assert replay.enqueue.outbox_id == original.enqueue.outbox_id
    assert source.calls == policy_source.calls == robot.calls == 0
    reopened.close()


def test_caller_constructed_dtos_and_foreign_capability_proof_fail(
    tmp_path,
    monkeypatch,
) -> None:
    """Public structural DTOs are not server execution authority."""
    values = _components(tmp_path / 'authority-forgery.sqlite3', monkeypatch)
    store, _, verifier, clock, _, source, _, _ = values
    draft, target, terminal = _resolve(store, clock, suffix='forgery')
    forged_approval = VerifiedSimulationApproval(
        user_id=_USER_ID,
        principal_binding_digest=simulation_tests._digest('forged-actor'),
        confirmation_request_id=draft.confirmation_request_id,
        confirmation_result_id=terminal.confirmation_result_id,
        proposal_fingerprint=draft.proposal_fingerprint,
        verified_at=terminal.resolved_at,
        expires_at=terminal.expires_at,
    )
    forged_request = SimulationConsumeRequest(
        consume_request_id='gazebo-simulation-consume-forged',
        confirmation_request_id=draft.confirmation_request_id,
        confirmation_result_id=terminal.confirmation_result_id,
        proposal_fingerprint=draft.proposal_fingerprint,
        current_target=target,
        target_observed_at=terminal.resolved_at,
        target_evidence_expires_at=terminal.expires_at,
    )

    with pytest.raises(SimulationAssuranceError) as missing:
        store.consume_approved_monitor_room_gazebo_simulation(
            approval=forged_approval,
            request=forged_request,
        )
    assert missing.value.__cause__ is None

    approval, request = verifier._issue(terminal)
    foreign = ServerGazeboSimulationExecutionVerifier(
        hashlib.sha256(b'foreign-process-capability').digest(),
        user_id=_USER_ID,
        semantic_evidence_source=source,
        clock=clock,
    )
    foreign_approval, foreign_request = foreign._issue(terminal)
    assert foreign_approval == approval
    with pytest.raises(SimulationAssuranceError) as wrong_proof:
        verifier.verify_receipt(foreign_approval, foreign_request, clock.now)
    assert wrong_proof.value.__cause__ is None
    assert request.trust_proof != foreign_request.trust_proof
    assert source.calls == 0
    store.close()


@pytest.mark.parametrize('drift', ('room', 'map', 'zones'))
def test_semantic_drift_is_rejected_before_policy_or_planning(
    tmp_path,
    monkeypatch,
    drift,
) -> None:
    """Fresh room, map, and zone bindings must equal the approved target."""
    semantics = _room_payload()
    if drift == 'room':
        room = semantics['userMap']['features'][0]
        room['properties']['name'] = '변경된 거실'
    elif drift == 'map':
        semantics['mapRevision'] = 'grid-revision-2'
        semantics['userMap']['map_revision'] = 'grid-revision-2'
    else:
        semantics['zones'] = _zones_payload()
    drift_source = StaticSemanticSource(
        _semantic_evidence(semantics=semantics)
    )
    stable_policy_source = StaticSemanticSource(_semantic_evidence())
    values = _components(
        tmp_path / f'authority-{drift}.sqlite3',
        monkeypatch,
        semantic_source=drift_source,
        policy_semantic_source=stable_policy_source,
    )
    store, consumer, _, clock, _, source, policy_source, robot = values
    draft, _, _ = _resolve(store, clock, suffix=drift)

    with pytest.raises(SimulationAssuranceError) as caught:
        consumer.consume(draft.confirmation_request_id)

    assert caught.value.__cause__ is None
    assert source.calls == 1
    assert policy_source.calls == robot.calls == 0
    assert store._connection.execute(
        'SELECT COUNT(*) FROM monitor_room_simulation_ledger'
    ).fetchone()[0] == 0
    store.close()


def test_wrong_user_denial_and_expiry_never_issue_simulation(
    tmp_path,
    monkeypatch,
) -> None:
    """Only a current approval owned by the configured principal qualifies."""
    values = _components(tmp_path / 'authority-reject.sqlite3', monkeypatch)
    store, consumer, _, clock, _, source, policy_source, robot = values
    denied, _, _ = _resolve(
        store,
        clock,
        suffix='denied',
        disposition='deny',
    )
    approved, _, terminal = _resolve(
        store,
        clock,
        suffix='expired',
    )

    with pytest.raises(SimulationAssuranceError):
        consumer.consume(denied.confirmation_request_id)

    clock.now = terminal.expires_at
    with pytest.raises(SimulationAssuranceError):
        consumer.consume(approved.confirmation_request_id)

    with pytest.raises(Exception) as wrong_user:
        store.get_confirmation_intent(
            'different-user',
            approved.confirmation_request_id,
        )
    assert 'different-user' not in str(wrong_user.value)
    assert source.calls == policy_source.calls == robot.calls == 0
    assert store._connection.execute(
        'SELECT COUNT(*) FROM monitor_room_gazebo_execution_outbox'
    ).fetchone()[0] == 0
    store.close()


def test_resolution_and_collaborator_mutation_fail_closed(
    tmp_path,
    monkeypatch,
) -> None:
    """Every durable actor field and configured collaborator is rechecked."""
    values = _components(tmp_path / 'authority-mutation.sqlite3', monkeypatch)
    store, consumer, verifier, clock, _, source, _, _ = values
    draft, _, terminal = _resolve(store, clock, suffix='mutation')
    object.__setattr__(terminal, 'response_channel', 'voice')

    with pytest.raises(SimulationAssuranceError) as resolution:
        verifier._issue(terminal)
    assert resolution.value.__cause__ is None
    with pytest.raises(AttributeError):
        verifier._semantic_source = object()
    with pytest.raises(AttributeError):
        consumer._store = object()

    other = ServerGazeboSimulationExecutionVerifier(
        _CAPABILITY,
        user_id=_USER_ID,
        semantic_evidence_source=source,
        clock=clock,
    )
    object.__setattr__(store, '_simulation_execution_verifier', other)
    with pytest.raises(SimulationAssuranceError):
        consumer.consume(draft.confirmation_request_id)
    assert source.calls == 0
    store.close()


def test_constructors_do_not_fetch_semantics_or_import_ros(
    tmp_path,
    monkeypatch,
) -> None:
    """Authority configuration is inert and contains no ROS surface."""
    values = _components(tmp_path / 'authority-inert.sqlite3', monkeypatch)
    store, _, verifier, _, _, source, policy_source, robot = values

    assert source.calls == policy_source.calls == robot.calls == 0
    assert not hasattr(verifier, 'navigate_to_pose')
    assert not hasattr(verifier, 'cancel_goal')
    assert not hasattr(verifier, '__dict__')
    store.close()


def test_external_seals_reject_co_mutation_and_method_shadowing(
    tmp_path,
    monkeypatch,
) -> None:
    """Object-level rewiring cannot replace production trust roots."""
    values = _components(tmp_path / 'authority-seal.sqlite3', monkeypatch)
    store, consumer, verifier, clock, _, source, _, _ = values
    draft, _, _ = _resolve(store, clock, suffix='seal')
    substitute = StaticSemanticSource(_semantic_evidence())

    with pytest.raises(AttributeError):
        object.__setattr__(verifier, 'verify', lambda *_args: None)
    with pytest.raises(AttributeError):
        object.__setattr__(consumer, 'consume', lambda *_args: None)

    object.__setattr__(verifier, '_semantic_source', substitute)
    object.__setattr__(
        verifier,
        '_fetch_semantic',
        substitute.fetch_snapshot_evidence,
    )
    object.__setattr__(verifier, '_capability', b'Z' * 32)
    with pytest.raises(SimulationAssuranceError) as verifier_changed:
        consumer.consume(draft.confirmation_request_id)
    assert verifier_changed.value.__cause__ is None
    assert source.calls == substitute.calls == 0
    assert store._connection.execute(
        'SELECT COUNT(*) FROM monitor_room_gazebo_execution_outbox'
    ).fetchone()[0] == 0
    store.close()

    other_values = _components(
        tmp_path / 'authority-consumer-seal.sqlite3',
        monkeypatch,
    )
    other_store, other_consumer, other_verifier, other_clock = (
        other_values[:4]
    )
    other_source = other_values[5]
    other_draft, _, _ = _resolve(
        other_store,
        other_clock,
        suffix='consumer-seal',
    )
    object.__setattr__(other_consumer, '_store', store)
    object.__setattr__(other_consumer, '_verifier', verifier)
    object.__setattr__(other_consumer, '_semantic_source', source)
    with pytest.raises(SimulationAssuranceError) as consumer_changed:
        other_consumer.consume(other_draft.confirmation_request_id)
    assert consumer_changed.value.__cause__ is None
    assert other_source.calls == 0
    assert type(other_verifier) is ServerGazeboSimulationExecutionVerifier
    other_store.close()
