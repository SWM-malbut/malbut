"""Atomic, durable Gazebo-simulation execution outbox tests."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import sqlite3

import pytest

import malbut_agent_server.execution_ledger as execution_ledger
import malbut_agent_server.gazebo_execution_outbox as outbox_module
import test_monitor_room_simulation_execution as simulation_tests
from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.gazebo_execution_outbox import (
    GAZEBO_EXECUTION_OUTBOX_MAX_SAMPLES,
    GazeboExecutionOutboxAssuranceError,
    GazeboExecutionOutboxConflictError,
    GazeboExecutionOutboxSchemaError,
    GazeboExecutionOutboxUpgradeRequiredError,
    GazeboSimulationExecutionPolicy,
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
from malbut_agent_server.schemas import ValidationError
from test_homecam_semantic import _Transport, _config
from test_robot_state import _BOOT_ID, _NONCE_A, _NOW_NS, _envelope


class BootClock:
    """Deterministic trusted test BOOTTIME source."""

    def __init__(self, now_ns: int = _NOW_NS) -> None:
        """Start at one exact trusted BOOTTIME sample."""
        self.now_ns = now_ns

    def __call__(self) -> int:
        """Return the current deterministic BOOTTIME sample."""
        return self.now_ns


class StaticSemanticSource:
    """Return a detached resolver-issued semantic evidence object."""

    def __init__(self, evidence) -> None:
        """Keep one resolver-issued evidence baseline."""
        self.evidence = evidence
        self.calls = 0

    def fetch_snapshot_evidence(self):
        """Return a newly detached canonical evidence copy."""
        self.calls += 1
        return self.evidence.canonical_copy()


class StaticRobotStateSource:
    """Return one exact trusted robot-state evidence object."""

    def __init__(self, evidence) -> None:
        """Keep one exact trusted robot snapshot."""
        self.evidence = evidence
        self.calls = 0

    def read(self):
        """Return the configured trusted robot snapshot."""
        self.calls += 1
        return self.evidence


def _semantic_evidence():
    return AuthenticatedHomecamSemanticResolver(
        _config(),
        transport=_Transport(),
        clock=lambda: 1002.0,
    ).fetch_snapshot_evidence()


def _robot_evidence(target, *, valid_until_ns=_NOW_NS + 1_000_000_000):
    value = _envelope(valid_until_ns=valid_until_ns)
    value['binding']['map_id'] = target.map_id
    value['binding']['map_revision'] = target.map_revision
    return parse_trusted_robot_state_envelope(
        value,
        expected_nonce=_NONCE_A,
        expected_device_id=target.device_id,
        expected_host_boot_id=_BOOT_ID,
        now_boottime_ns=_NOW_NS,
    )


def _configured_store(
    database,
    monkeypatch,
    *,
    wall_clock=None,
    boot_clock=None,
    semantic_source=None,
    robot_source=None,
):
    wall = (
        simulation_tests.MutableClock(1002.0)
        if wall_clock is None else wall_clock
    )
    boot = BootClock() if boot_clock is None else boot_clock
    evidence = _semantic_evidence()
    target = resolve_monitor_room_target(
        evidence.snapshot,
        '거실',
        simulation_tests._target(
            '{"location":"거실"}', 'effects-only'
        ).effects,
    )
    semantic = (
        StaticSemanticSource(evidence)
        if semantic_source is None else semantic_source
    )
    robot = (
        StaticRobotStateSource(_robot_evidence(target))
        if robot_source is None else robot_source
    )
    policy = GazeboSimulationExecutionPolicy._for_test(
        robot_id=target.device_id,
        expected_device_id=target.device_id,
        semantic_evidence_source=semantic,
        robot_state_source=robot,
        expected_host_boot_id=_BOOT_ID,
        boottime_ns=boot,
    )
    monkeypatch.setattr(
        simulation_tests,
        '_target',
        lambda _arguments, _suffix: target,
    )
    store = SQLiteConversationStore(
        str(database),
        clock=wall,
        simulation_execution_verifier=(
            simulation_tests._TEST_TRUST
        ),
        gazebo_execution_policy=policy,
    )
    return store, wall, boot, target, semantic, robot, policy


def test_fresh_consume_atomically_preserves_exact_private_plan_and_replays(
    tmp_path,
    monkeypatch,
) -> None:
    """Store exact private samples while public results stay redacted."""
    database = tmp_path / 'gazebo-outbox.sqlite3'
    store, wall, boot, target, semantic, robot, _policy = (
        _configured_store(database, monkeypatch)
    )
    scenario = simulation_tests._scenario(
        store, wall, suffix='gazebo-happy'
    )

    result = store.consume_approved_monitor_room_gazebo_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )

    assert result.receipt.record_kind == 'planned'
    assert result.enqueue is not None
    assert result.enqueue.operation_id.startswith('gazebo-operation-')
    assert result.enqueue.operation_id != result.receipt.operation_id
    assert result.enqueue.sample_count <= GAZEBO_EXECUTION_OUTBOX_MAX_SAMPLES
    public = result.to_public_dict()
    assert 'x_mm' not in str(public)
    assert target.device_id not in str(public)
    row = store._connection.execute(
        'SELECT * FROM monitor_room_gazebo_execution_outbox'
    ).fetchone()
    samples = store._connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_execution_samples
        ORDER BY sample_index
        '''
    ).fetchall()
    assert row['source_receipt_digest'] == result.receipt.receipt_digest
    assert row['zones_digest'] == semantic.evidence.snapshot.zones_digest
    assert row['robot_id'] == target.device_id
    assert row['host_boot_id'] == _BOOT_ID
    assert row['physical_authorized'] == 0
    assert row['physical_effects'] == 0
    assert len(samples) == result.enqueue.sample_count
    assert [sample['sample_index'] for sample in samples] == list(
        range(len(samples))
    )
    assert semantic.calls == robot.calls == 1

    replay = store.consume_approved_monitor_room_gazebo_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    assert replay.receipt.replayed is True
    assert replay.enqueue is not None
    assert replay.enqueue.replayed is True
    assert replay.enqueue.outbox_id == result.enqueue.outbox_id
    assert semantic.calls == robot.calls == 1
    assert store._connection.execute(
        'SELECT COUNT(*) FROM monitor_room_gazebo_execution_outbox'
    ).fetchone()[0] == 1

    claim = store.claim_gazebo_execution(
        'gazebo-claim-happy', lease_seconds=30
    )
    assert claim is not None
    assert claim.host_boot_id == _BOOT_ID
    assert claim.operation_id == result.enqueue.operation_id
    assert len(claim.ordered_semantic_samples) == len(samples)
    prepare = claim.to_private_prepare_dict()
    assert prepare['deadline'] == row['deadline_boottime_ns'] / 1e9
    assert prepare['host_boot_id'] == _BOOT_ID
    assert prepare['ordered_semantic_samples'][0]['x_mm'] == samples[0]['x_mm']
    claim_replay = store.claim_gazebo_execution(
        'gazebo-claim-happy', lease_seconds=30
    )
    assert claim_replay == claim

    prepared_fingerprint = hashlib.sha256(b'gazebo-prepare').hexdigest()
    acknowledgement = store.acknowledge_gazebo_execution(
        outbox_id=claim.outbox_id,
        claim_token=claim.claim_token,
        claim_fence=claim.claim_fence,
        prepare_fingerprint=prepared_fingerprint,
    )
    assert acknowledgement.state == 'prepared'
    assert acknowledgement.prepare_fingerprint == prepared_fingerprint
    assert store.claim_gazebo_execution('gazebo-claim-after-ack') is None
    store.close()


def test_existing_pure_planned_receipt_cannot_be_elevated(
    tmp_path,
    monkeypatch,
) -> None:
    """Never promote a terminal receipt created by the pure API."""
    store, wall, _boot, _target, semantic, robot, _policy = (
        _configured_store(
            tmp_path / 'gazebo-no-upgrade.sqlite3', monkeypatch
        )
    )
    scenario = simulation_tests._scenario(
        store, wall, suffix='gazebo-no-upgrade'
    )
    pure = store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    assert pure.record_kind == 'planned'

    with pytest.raises(
        GazeboExecutionOutboxUpgradeRequiredError,
        match='cannot be elevated',
    ):
        store.consume_approved_monitor_room_gazebo_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )

    assert semantic.calls == robot.calls == 0
    assert store._connection.execute(
        'SELECT COUNT(*) FROM monitor_room_gazebo_execution_outbox'
    ).fetchone()[0] == 0
    store.close()


def test_final_boottime_expiry_rolls_back_receipt_outbox_and_samples(
    tmp_path,
    monkeypatch,
) -> None:
    """Roll back every row when final robot evidence has expired."""
    boot = BootClock()
    store, wall, _boot, _target, semantic, robot, _policy = (
        _configured_store(
            tmp_path / 'gazebo-final-expiry.sqlite3',
            monkeypatch,
            boot_clock=boot,
        )
    )
    scenario = simulation_tests._scenario(
        store, wall, suffix='gazebo-final-expiry'
    )
    original = outbox_module.record_gazebo_execution_outbox_locked

    def expire_before_record(*args, **kwargs):
        boot.now_ns = _NOW_NS + 2_000_000_000
        return original(*args, **kwargs)

    monkeypatch.setattr(
        'malbut_agent_server.conversation.'
        'record_gazebo_execution_outbox_locked',
        expire_before_record,
    )
    with pytest.raises(
        GazeboExecutionOutboxAssuranceError,
        match='evidence changed|expired',
    ):
        store.consume_approved_monitor_room_gazebo_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )
    assert semantic.calls == robot.calls == 1
    for table in (
        'monitor_room_simulation_ledger',
        'monitor_room_gazebo_execution_outbox',
        'monitor_room_gazebo_execution_samples',
    ):
        assert store._connection.execute(
            f'SELECT COUNT(*) FROM {table}'
        ).fetchone()[0] == 0
    store.close()


@pytest.mark.parametrize('lifecycle', ['reset', 'delete'])
def test_created_outbox_survives_conversation_reset_delete_and_restart(
    tmp_path,
    monkeypatch,
    lifecycle,
) -> None:
    """Retain created execution evidence across reset/delete and restart."""
    database = tmp_path / f'gazebo-survive-{lifecycle}.sqlite3'
    store, wall, _boot, _target, _semantic, _robot, _policy = (
        _configured_store(database, monkeypatch)
    )
    scenario = simulation_tests._scenario(
        store, wall, suffix=f'gazebo-survive-{lifecycle}'
    )
    result = store.consume_approved_monitor_room_gazebo_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    if lifecycle == 'reset':
        store.reset(
            scenario.draft.user_id,
            scenario.draft.conversation_id,
        )
    else:
        assert store.delete(
            scenario.draft.user_id,
            scenario.draft.conversation_id,
        )
    assert store._connection.execute(
        'SELECT COUNT(*) FROM monitor_room_gazebo_execution_outbox'
    ).fetchone()[0] == 1
    assert store._connection.execute(
        'SELECT COUNT(*) FROM monitor_room_gazebo_execution_samples'
    ).fetchone()[0] == result.enqueue.sample_count
    store.close()

    reopened, _wall, _boot, _target, _semantic, _robot, _policy = (
        _configured_store(database, monkeypatch)
    )
    claim = reopened.claim_gazebo_execution(
        f'gazebo-restart-claim-{lifecycle}'
    )
    assert claim is not None
    assert claim.outbox_id == result.enqueue.outbox_id
    assert claim.operation_id == result.enqueue.operation_id
    reopened.close()


def test_claim_attempt_bound_becomes_typed_terminal_without_resurrection(
    tmp_path,
    monkeypatch,
) -> None:
    """Terminalize an exhausted lease sequence without resurrection."""
    boot = BootClock()
    store, wall, _boot, _target, _semantic, _robot, _policy = (
        _configured_store(
            tmp_path / 'gazebo-attempt-bound.sqlite3',
            monkeypatch,
            boot_clock=boot,
        )
    )
    scenario = simulation_tests._scenario(
        store, wall, suffix='gazebo-attempt-bound'
    )
    store.consume_approved_monitor_room_gazebo_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    claims = []
    for index in range(outbox_module.GAZEBO_EXECUTION_OUTBOX_MAX_ATTEMPTS):
        claim = store.claim_gazebo_execution(
            f'gazebo-bounded-claim-{index}', lease_seconds=1
        )
        assert claim is not None
        claims.append(claim)
        boot.now_ns += 1_000_000_001
    assert [claim.claim_fence for claim in claims] == list(
        range(1, len(claims) + 1)
    )
    assert store.claim_gazebo_execution(
        'gazebo-bounded-claim-terminal', lease_seconds=1
    ) is None
    row = store._connection.execute(
        'SELECT * FROM monitor_room_gazebo_execution_outbox'
    ).fetchone()
    assert row['state'] == 'expired'
    assert row['terminal_code'] == 'delivery_attempts_exhausted'
    with pytest.raises(
        GazeboExecutionOutboxConflictError,
        match='no longer current',
    ):
        store.claim_gazebo_execution(
            claims[-1].claim_request_id, lease_seconds=1
        )
    store.close()


def test_post_outbox_fault_rolls_back_source_samples_and_feedback(
    tmp_path,
    monkeypatch,
) -> None:
    """Roll back receipt and samples when later feedback insertion fails."""
    store, wall, _boot, _target, _semantic, _robot, _policy = (
        _configured_store(
            tmp_path / 'gazebo-post-outbox-fault.sqlite3', monkeypatch
        )
    )
    scenario = simulation_tests._scenario(
        store, wall, suffix='gazebo-post-outbox-fault'
    )

    def fail_after_outbox(*_args, **_kwargs):
        raise RuntimeError('injected trusted result fault')

    monkeypatch.setattr(
        'malbut_agent_server.conversation.'
        'record_or_verify_trusted_result_locked',
        fail_after_outbox,
    )
    with pytest.raises(RuntimeError, match='injected trusted result fault'):
        store.consume_approved_monitor_room_gazebo_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )
    for table in (
        'monitor_room_simulation_ledger',
        'monitor_room_gazebo_execution_outbox',
        'monitor_room_gazebo_execution_samples',
        'conversation_trusted_tool_results',
        'trusted_result_tts_outbox',
    ):
        assert store._connection.execute(
            f'SELECT COUNT(*) FROM {table}'
        ).fetchone()[0] == 0
    store.close()


def test_sqlite_failure_is_rolled_back_and_redacted_at_public_boundary(
    tmp_path,
    monkeypatch,
) -> None:
    """Hide raw SQLite details and exception chains after rollback."""
    store, wall, _boot, _target, _semantic, _robot, _policy = (
        _configured_store(
            tmp_path / 'gazebo-sqlite-redaction.sqlite3', monkeypatch
        )
    )
    scenario = simulation_tests._scenario(
        store, wall, suffix='gazebo-sqlite-redaction'
    )

    def fail_with_private_sql(*_args, **_kwargs):
        raise sqlite3.OperationalError(
            'private INSERT statement and bound values'
        )

    monkeypatch.setattr(
        'malbut_agent_server.conversation.'
        'record_gazebo_execution_outbox_locked',
        fail_with_private_sql,
    )
    with pytest.raises(ValidationError) as raised:
        store.consume_approved_monitor_room_gazebo_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )
    assert str(raised.value) == 'Gazebo durable enqueue storage failed'
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert 'private INSERT' not in repr(raised.value)
    for table in (
        'monitor_room_simulation_ledger',
        'monitor_room_gazebo_execution_outbox',
        'monitor_room_gazebo_execution_samples',
    ):
        assert store._connection.execute(
            f'SELECT COUNT(*) FROM {table}'
        ).fetchone()[0] == 0
    store.close()


def test_mutated_fixed_policy_is_rejected_before_receipt_creation(
    tmp_path,
    monkeypatch,
) -> None:
    """Reject object-level mutation of the fixed trust policy."""
    store, wall, _boot, _target, _semantic, _robot, policy = (
        _configured_store(
            tmp_path / 'gazebo-policy-seal.sqlite3', monkeypatch
        )
    )
    scenario = simulation_tests._scenario(
        store, wall, suffix='gazebo-policy-seal'
    )
    object.__setattr__(policy, '_robot_id', 'changed-device')

    with pytest.raises(
        GazeboExecutionOutboxAssuranceError,
        match='configuration changed',
    ):
        store.consume_approved_monitor_room_gazebo_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )
    assert store._connection.execute(
        'SELECT COUNT(*) FROM monitor_room_simulation_ledger'
    ).fetchone()[0] == 0
    assert store._connection.execute(
        'SELECT COUNT(*) FROM monitor_room_gazebo_execution_outbox'
    ).fetchone()[0] == 0
    store.close()


def _drop_outbox_schema(database, *, remove_anchor: bool) -> None:
    connection = sqlite3.connect(str(database))
    try:
        objects = connection.execute(
            '''
            SELECT type, name FROM sqlite_master
            WHERE name IN (
                SELECT name FROM sqlite_master
                WHERE name LIKE 'monitor_room_gazebo_%'
            )
              AND type IN ('trigger', 'index')
            ORDER BY CASE type WHEN 'trigger' THEN 0 ELSE 1 END
            '''
        ).fetchall()
        for object_type, name in objects:
            connection.execute(
                f'DROP {object_type.upper()} "{name}"'
            )
        for table in (
            'monitor_room_gazebo_execution_acknowledgements',
            'monitor_room_gazebo_execution_claims',
            'monitor_room_gazebo_execution_samples',
            'monitor_room_gazebo_execution_outbox',
            'monitor_room_gazebo_outbox_preactivation_sources',
            'monitor_room_gazebo_outbox_schema_metadata',
        ):
            connection.execute(f'DROP TABLE "{table}"')
        if remove_anchor:
            for trigger in (
                'monitor_room_simulation_preactivation_no_update',
                'monitor_room_simulation_preactivation_no_delete',
                'monitor_room_simulation_preactivation_no_insert',
            ):
                connection.execute(f'DROP TRIGGER "{trigger}"')
            connection.execute(
                '''
                DELETE FROM monitor_room_simulation_preactivation_proposals
                WHERE proposal_fingerprint = ?
                ''',
                (
                    outbox_module
                    .GAZEBO_EXECUTION_OUTBOX_ACTIVATION_SENTINEL,
                ),
            )
            connection.execute(
                execution_ledger
                .SIMULATION_PREACTIVATION_NO_UPDATE_TRIGGER_SQL
            )
            connection.execute(
                execution_ledger
                .SIMULATION_PREACTIVATION_NO_DELETE_TRIGGER_SQL
            )
            connection.execute(
                execution_ledger
                .SIMULATION_PREACTIVATION_NO_INSERT_TRIGGER_SQL
            )
        connection.commit()
    finally:
        connection.close()


def test_activation_snapshots_old_receipt_and_never_backfills(
    tmp_path,
    monkeypatch,
) -> None:
    """Snapshot old receipts at activation and never create an outbox."""
    database = tmp_path / 'gazebo-preactivation.sqlite3'
    store, wall, _boot, _target, _semantic, _robot, _policy = (
        _configured_store(database, monkeypatch)
    )
    scenario = simulation_tests._scenario(
        store, wall, suffix='gazebo-preactivation'
    )
    store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    store.close()
    # Model a database created before this feature existed: remove both the
    # feature schema and its external activation sentinel, then activate it
    # for the first time around the already-terminal pure receipt.
    _drop_outbox_schema(database, remove_anchor=True)
    reopened, _wall, _boot, _target, semantic, robot, _policy = (
        _configured_store(database, monkeypatch)
    )
    metadata = reopened._connection.execute(
        'SELECT * FROM monitor_room_gazebo_outbox_schema_metadata'
    ).fetchone()
    assert metadata['preactivation_count'] == 1
    assert reopened._connection.execute(
        'SELECT COUNT(*) FROM '
        'monitor_room_gazebo_outbox_preactivation_sources'
    ).fetchone()[0] == 1
    with pytest.raises(GazeboExecutionOutboxUpgradeRequiredError):
        reopened.consume_approved_monitor_room_gazebo_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )
    assert semantic.calls == robot.calls == 0
    assert reopened._connection.execute(
        'SELECT COUNT(*) FROM monitor_room_gazebo_execution_outbox'
    ).fetchone()[0] == 0
    reopened.close()


def test_activation_sentinel_detects_removed_outbox_schema(
    tmp_path,
    monkeypatch,
) -> None:
    """Detect complete feature-schema removal through its external anchor."""
    database = tmp_path / 'gazebo-schema-removed.sqlite3'
    store, _wall, _boot, _target, _semantic, _robot, _policy = (
        _configured_store(database, monkeypatch)
    )
    store.close()
    _drop_outbox_schema(database, remove_anchor=False)

    with pytest.raises(
        GazeboExecutionOutboxSchemaError,
        match='removed after activation',
    ):
        _configured_store(database, monkeypatch)


def test_semantic_ttl_expiry_at_final_record_boundary_rolls_back(
    tmp_path,
    monkeypatch,
) -> None:
    """Recheck semantic TTL immediately before the durable insert."""
    store, wall, _boot, _target, semantic, robot, _policy = (
        _configured_store(
            tmp_path / 'gazebo-semantic-final-expiry.sqlite3',
            monkeypatch,
        )
    )
    scenario = simulation_tests._scenario(
        store, wall, suffix='gazebo-semantic-final-expiry'
    )
    original_fetch = semantic.fetch_snapshot_evidence

    def expire_semantics_after_fetch():
        evidence = original_fetch()
        wall.now = 1006.0
        return evidence

    monkeypatch.setattr(
        semantic, 'fetch_snapshot_evidence', expire_semantics_after_fetch
    )
    with pytest.raises(
        GazeboExecutionOutboxAssuranceError,
        match='semantic evidence expired',
    ):
        store.consume_approved_monitor_room_gazebo_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )
    assert semantic.calls == robot.calls == 1
    assert store._connection.execute(
        'SELECT COUNT(*) FROM monitor_room_simulation_ledger'
    ).fetchone()[0] == 0
    assert store._connection.execute(
        'SELECT COUNT(*) FROM monitor_room_gazebo_execution_outbox'
    ).fetchone()[0] == 0
    store.close()


def test_expired_claim_and_old_fence_cannot_ack_new_prepare(
    tmp_path,
    monkeypatch,
) -> None:
    """Reject expired and superseded claim credentials during ACK."""
    boot = BootClock()
    store, wall, _boot, _target, _semantic, _robot, _policy = (
        _configured_store(
            tmp_path / 'gazebo-stale-ack.sqlite3',
            monkeypatch,
            boot_clock=boot,
        )
    )
    scenario = simulation_tests._scenario(
        store, wall, suffix='gazebo-stale-ack'
    )
    store.consume_approved_monitor_room_gazebo_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    first = store.claim_gazebo_execution(
        'gazebo-stale-first', lease_seconds=1
    )
    boot.now_ns += 1_000_000_001
    with pytest.raises(
        GazeboExecutionOutboxConflictError,
        match='lease expired',
    ):
        store.acknowledge_gazebo_execution(
            outbox_id=first.outbox_id,
            claim_token=first.claim_token,
            claim_fence=first.claim_fence,
            prepare_fingerprint=hashlib.sha256(b'old').hexdigest(),
        )
    second = store.claim_gazebo_execution(
        'gazebo-stale-second', lease_seconds=30
    )
    assert second.claim_fence == first.claim_fence + 1
    with pytest.raises(
        GazeboExecutionOutboxConflictError,
        match='stale',
    ):
        store.acknowledge_gazebo_execution(
            outbox_id=first.outbox_id,
            claim_token=first.claim_token,
            claim_fence=first.claim_fence,
            prepare_fingerprint=hashlib.sha256(b'old').hexdigest(),
        )
    store.close()


def test_targeted_claim_never_touches_an_older_pending_outbox(
    tmp_path,
    monkeypatch,
) -> None:
    """An explicit B claim leases B while an older A remains pending."""
    store, wall, _boot, _target, _semantic, _robot, _policy = (
        _configured_store(
            tmp_path / 'gazebo-targeted-backlog.sqlite3',
            monkeypatch,
        )
    )
    scenario_a = simulation_tests._scenario(
        store, wall, suffix='gazebo-targeted-a'
    )
    enqueue_a = store.consume_approved_monitor_room_gazebo_simulation(
        approval=scenario_a.approval,
        request=scenario_a.request,
    ).enqueue
    scenario_b = simulation_tests._scenario(
        store, wall, suffix='gazebo-targeted-b'
    )
    enqueue_b = store.consume_approved_monitor_room_gazebo_simulation(
        approval=scenario_b.approval,
        request=scenario_b.request,
    ).enqueue
    assert enqueue_a is not None and enqueue_b is not None

    claim = store.claim_gazebo_execution(
        'gazebo-targeted-claim-b',
        expected_outbox_id=enqueue_b.outbox_id,
        expected_operation_id=enqueue_b.operation_id,
        expected_confirmation_request_id=(
            scenario_b.draft.confirmation_request_id
        ),
    )

    assert claim is not None
    assert claim.outbox_id == enqueue_b.outbox_id
    assert claim.operation_id == enqueue_b.operation_id
    rows = store._connection.execute(
        '''
        SELECT outbox_id, state, attempt_count
        FROM monitor_room_gazebo_execution_outbox
        ORDER BY created_boottime_ns, outbox_id
        '''
    ).fetchall()
    states = {
        row['outbox_id']: (row['state'], row['attempt_count'])
        for row in rows
    }
    assert states[enqueue_a.outbox_id] == ('pending', 0)
    assert states[enqueue_b.outbox_id] == ('claimed', 1)

    replay = store.claim_gazebo_execution(
        'gazebo-targeted-claim-b',
        expected_outbox_id=enqueue_b.outbox_id,
        expected_operation_id=enqueue_b.operation_id,
        expected_confirmation_request_id=(
            scenario_b.draft.confirmation_request_id
        ),
    )
    assert replay == claim
    store.close()


def test_targeted_claim_rejects_wrong_or_incomplete_binding_without_touching(
    tmp_path,
    monkeypatch,
) -> None:
    """Target identity mismatch cannot fall back to the oldest row."""
    store, wall, _boot, _target, _semantic, _robot, _policy = (
        _configured_store(
            tmp_path / 'gazebo-targeted-wrong.sqlite3',
            monkeypatch,
        )
    )
    scenario = simulation_tests._scenario(
        store, wall, suffix='gazebo-targeted-wrong'
    )
    enqueue = store.consume_approved_monitor_room_gazebo_simulation(
        approval=scenario.approval,
        request=scenario.request,
    ).enqueue
    assert enqueue is not None

    with pytest.raises(
        GazeboExecutionOutboxAssuranceError,
        match='incomplete',
    ):
        store.claim_gazebo_execution(
            'gazebo-targeted-incomplete',
            expected_outbox_id=enqueue.outbox_id,
        )
    with pytest.raises(
        GazeboExecutionOutboxConflictError,
        match='conflicts',
    ):
        store.claim_gazebo_execution(
            'gazebo-targeted-wrong-operation',
            expected_outbox_id=enqueue.outbox_id,
            expected_operation_id='gazebo-operation-wrong',
            expected_confirmation_request_id=(
                scenario.draft.confirmation_request_id
            ),
        )
    with pytest.raises(
        GazeboExecutionOutboxConflictError,
        match='conflicts',
    ):
        store.claim_gazebo_execution(
            'gazebo-targeted-wrong-confirmation',
            expected_outbox_id=enqueue.outbox_id,
            expected_operation_id=enqueue.operation_id,
            expected_confirmation_request_id=(
                'simulation-confirmation-wrong'
            ),
        )
    with pytest.raises(
        GazeboExecutionOutboxConflictError,
        match='not found',
    ):
        store.claim_gazebo_execution(
            'gazebo-targeted-missing-outbox',
            expected_outbox_id='gazebo-execution-outbox-missing',
            expected_operation_id=enqueue.operation_id,
            expected_confirmation_request_id=(
                scenario.draft.confirmation_request_id
            ),
        )
    row = store._connection.execute(
        'SELECT state, attempt_count '
        'FROM monitor_room_gazebo_execution_outbox'
    ).fetchone()
    assert (row['state'], row['attempt_count']) == ('pending', 0)
    store.close()


def test_two_connections_race_only_for_the_same_target_and_replay_exactly(
    tmp_path,
    monkeypatch,
) -> None:
    """Concurrent claim IDs cannot redirect one targeted operation."""
    database = tmp_path / 'gazebo-targeted-race.sqlite3'
    first, wall, _boot, _target, _semantic, _robot, _policy = (
        _configured_store(database, monkeypatch)
    )
    scenario_a = simulation_tests._scenario(
        first, wall, suffix='gazebo-targeted-race-a'
    )
    enqueue_a = first.consume_approved_monitor_room_gazebo_simulation(
        approval=scenario_a.approval,
        request=scenario_a.request,
    ).enqueue
    scenario_b = simulation_tests._scenario(
        first, wall, suffix='gazebo-targeted-race-b'
    )
    enqueue_b = first.consume_approved_monitor_room_gazebo_simulation(
        approval=scenario_b.approval,
        request=scenario_b.request,
    ).enqueue
    assert enqueue_a is not None and enqueue_b is not None
    second, *_unused = _configured_store(
        database,
        monkeypatch,
        wall_clock=wall,
    )

    def claim(store, request_id):
        return store.claim_gazebo_execution(
            request_id,
            expected_outbox_id=enqueue_b.outbox_id,
            expected_operation_id=enqueue_b.operation_id,
            expected_confirmation_request_id=(
                scenario_b.draft.confirmation_request_id
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(claim, first, 'gazebo-race-one'),
            executor.submit(claim, second, 'gazebo-race-two'),
        )
        results = tuple(future.result(timeout=5) for future in futures)
    issued = tuple(result for result in results if result is not None)
    assert len(issued) == 1
    assert issued[0].outbox_id == enqueue_b.outbox_id
    winner_request = issued[0].claim_request_id
    replay = second.claim_gazebo_execution(
        winner_request,
        expected_outbox_id=enqueue_b.outbox_id,
        expected_operation_id=enqueue_b.operation_id,
        expected_confirmation_request_id=(
            scenario_b.draft.confirmation_request_id
        ),
    )
    assert replay == issued[0]
    rows = first._connection.execute(
        'SELECT outbox_id, state FROM '
        'monitor_room_gazebo_execution_outbox'
    ).fetchall()
    states = {row['outbox_id']: row['state'] for row in rows}
    assert states[enqueue_a.outbox_id] == 'pending'
    assert states[enqueue_b.outbox_id] == 'claimed'
    second.close()
    first.close()


def test_deadline_expiry_is_terminal_and_exact_replay_does_not_renew(
    tmp_path,
    monkeypatch,
) -> None:
    """Keep one immutable deadline terminal across exact consume replay."""
    boot = BootClock()
    store, wall, _boot, _target, semantic, robot, _policy = (
        _configured_store(
            tmp_path / 'gazebo-deadline.sqlite3',
            monkeypatch,
            boot_clock=boot,
        )
    )
    scenario = simulation_tests._scenario(
        store, wall, suffix='gazebo-deadline'
    )
    first = store.consume_approved_monitor_room_gazebo_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    original_deadline = first.enqueue.deadline_boottime_ns
    boot.now_ns = original_deadline

    assert store.claim_gazebo_execution(
        'gazebo-deadline-claim'
    ) is None
    row = store._connection.execute(
        'SELECT * FROM monitor_room_gazebo_execution_outbox'
    ).fetchone()
    assert row['state'] == 'expired'
    assert row['terminal_code'] == 'deadline_expired'
    replay = store.consume_approved_monitor_room_gazebo_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    assert replay.enqueue.state == 'expired'
    assert replay.enqueue.deadline_boottime_ns == original_deadline
    assert semantic.calls == robot.calls == 1
    store.close()


def test_private_sample_tamper_is_detected_before_restart_claim(
    tmp_path,
    monkeypatch,
) -> None:
    """Detect coordinate drift before a restarted worker can claim it."""
    database = tmp_path / 'gazebo-sample-tamper.sqlite3'
    store, wall, _boot, _target, _semantic, _robot, _policy = (
        _configured_store(database, monkeypatch)
    )
    scenario = simulation_tests._scenario(
        store, wall, suffix='gazebo-sample-tamper'
    )
    store.consume_approved_monitor_room_gazebo_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    store.close()

    connection = sqlite3.connect(str(database))
    try:
        connection.execute(
            'DROP TRIGGER monitor_room_gazebo_sample_no_update'
        )
        connection.execute(
            '''
            UPDATE monitor_room_gazebo_execution_samples
            SET x_mm = x_mm + 500
            WHERE sample_index = 0
            '''
        )
        connection.execute(
            outbox_module.GAZEBO_OUTBOX_SAMPLE_NO_UPDATE_SQL
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        GazeboExecutionOutboxSchemaError,
        match='sample digest changed',
    ):
        _configured_store(database, monkeypatch)


def test_outbox_uses_the_exact_shared_planner_sample_bound() -> None:
    """Share the planner's complete whole-room sample capacity."""
    assert GAZEBO_EXECUTION_OUTBOX_MAX_SAMPLES == (
        outbox_module.DEFAULT_COVERAGE_PROFILE.max_samples
    )
    assert GAZEBO_EXECUTION_OUTBOX_MAX_SAMPLES == 4096
