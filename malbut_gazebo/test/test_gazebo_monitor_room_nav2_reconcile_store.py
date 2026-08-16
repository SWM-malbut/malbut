"""Tests for the private durable Nav2 unknown-goal reconciliation sidecar."""

import hashlib
import sqlite3

import pytest

from malbut_gazebo.gazebo_monitor_room_nav2_reconcile_store import (
    _case_digest,
    _EVENT_NO_UPDATE_SQL,
    _lease_event_evidence,
    GazeboMonitorRoomNav2ReconcileClockRollbackError,
    GazeboMonitorRoomNav2ReconcileConflictError,
    GazeboMonitorRoomNav2ReconcileFenceError,
    GazeboMonitorRoomNav2ReconcileLeaseError,
    GazeboMonitorRoomNav2ReconcileSchemaError,
    GazeboMonitorRoomNav2ReconcileStore,
    GazeboMonitorRoomNav2ReconcileValidationError,
)
from malbut_gazebo.gazebo_monitor_room_store import (
    CancelOperation,
    GazeboMonitorRoomStore,
    OrderedSemanticSample,
    PrepareOperation,
)


_DIGEST_A = 'a' * 64
_DIGEST_B = 'b' * 64
_DIGEST_C = 'c' * 64
_BOOT_ONE = '11111111-2222-3333-4444-555555555555'
_BOOT_TWO = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


def _boot_one():
    return _BOOT_ONE


def _boot_two():
    return _BOOT_TWO


def _request(operation_id='operation-1', robot_id='robot-1'):
    return PrepareOperation(
        prepare_request_id=f'prepare-{operation_id}',
        operation_id=operation_id,
        robot_id=robot_id,
        map_id='home-map',
        map_revision='map-revision-1',
        semantic_revision='semantic-revision-1',
        zones_digest=_DIGEST_A,
        target_binding_digest=_DIGEST_A,
        effects_digest=_DIGEST_A,
        profile_digest=_DIGEST_A,
        plan_digest=_DIGEST_A,
        ordered_semantic_samples=(
            OrderedSemanticSample(0, 0, 0, 1000, 2000),
        ),
        deadline=100.0,
    )


def _core_unknown(tmp_path, *, source_state='delivery_unknown'):
    path = tmp_path / f'core-{source_state}.sqlite3'
    store = GazeboMonitorRoomStore(path, boot_id_reader=_boot_one)
    store.prepare(_request(), now=1.0)
    store.acquire_lease(
        'operation-1',
        worker_id='core-worker',
        expected_fence=0,
        lease_seconds=20.0,
        now=2.0,
    )
    token = store.transition_token(
        'operation-1', worker_id='core-worker'
    )
    store.begin_preflight(token, now=3.0)
    token = store.transition_token(
        'operation-1', worker_id='core-worker'
    )
    store.record_send_intent(
        token, preflight_digest=_DIGEST_A, now=4.0
    )
    token = store.transition_token(
        'operation-1', worker_id='core-worker'
    )
    store.record_navigating(
        token, acceptance_digest=_DIGEST_A, now=5.0
    )
    if source_state == 'delivery_unknown':
        store.record_delivery_unknown(
            store.transition_token(
                'operation-1', worker_id='core-worker'
            ),
            code='nav2_goal_not_observable',
            evidence_digest=_DIGEST_A,
            now=6.0,
        )
    else:
        store.request_cancel(
            CancelOperation(
                cancel_request_id='cancel-core-1',
                transition=store.transition_token(
                    'operation-1', worker_id='core-worker'
                ),
                reason_code='operator_requested',
            ),
            now=6.0,
        )
        store.record_cancel_unknown(
            store.transition_token(
                'operation-1', worker_id='core-worker'
            ),
            code='nav2_cancel_terminal_not_observable',
            evidence_digest=_DIGEST_A,
            now=7.0,
        )
    return store, path


def _sidecar(tmp_path, core, *, dwell=2.0, name='reconcile.sqlite3'):
    return GazeboMonitorRoomNav2ReconcileStore(
        tmp_path / name,
        core_store_namespace=core.store_namespace,
        quiescence_dwell_seconds=dwell,
        boot_id_reader=_boot_one,
    )


def _registered(tmp_path, *, source_state='delivery_unknown', dwell=2.0):
    core, core_path = _core_unknown(
        tmp_path, source_state=source_state
    )
    sidecar = _sidecar(tmp_path, core, dwell=dwell)
    observation = sidecar.register_unknown(core, 'operation-1', now=10.0)
    return core, core_path, sidecar, observation


def _lease(sidecar, *, now=11.0, worker='reconcile-worker', fence=0):
    return sidecar.acquire_lease(
        'operation-1',
        worker_id=worker,
        expected_fence=fence,
        lease_seconds=20.0,
        now=now,
    )


def _claim(
    sidecar,
    lease,
    *,
    attempt_id='observe-1',
    kind='observe',
    request=_DIGEST_B,
    wire=None,
    now=12.0,
):
    return sidecar.claim_attempt(
        'operation-1',
        attempt_id=attempt_id,
        kind=kind,
        worker_id=lease.worker_id,
        fence_epoch=lease.fence_epoch,
        request_fingerprint=request,
        wire_payload_digest=wire,
        now=now,
    )


def _terminal(sidecar, lease, *, status='succeeded', now=13.0):
    claim = _claim(sidecar, lease)
    return sidecar.record_goal_observation(
        claim.token,
        status=status,
        evidence_digest=_DIGEST_C,
        now=now,
    )


def _database_rows(path):
    connection = sqlite3.connect(path)
    tables = tuple(
        row[0]
        for row in connection.execute(
            '''
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            '''
        )
    )
    snapshot = tuple(
        (table, tuple(connection.execute(f'SELECT * FROM {table}')))
        for table in tables
    )
    connection.close()
    return snapshot


def test_registers_exact_unknown_anchor_without_changing_core(tmp_path):
    """Activation binds the final core event and preserves every core row."""
    core, core_path = _core_unknown(tmp_path)
    before = _database_rows(core_path)
    sidecar = _sidecar(tmp_path, core)

    observation = sidecar.register_unknown(core, 'operation-1', now=10.0)

    assert _database_rows(core_path) == before
    assert observation.source_state == 'delivery_unknown'
    assert observation.state == 'blocked_unresolved'
    assert observation.robot_blocked is True
    assert observation.operation_success is False
    assert observation.coverage_achieved is False
    assert observation.core_admission_released is False
    assert observation.to_public_dict()['safe_block_released'] is False
    anchor = sidecar.source_anchor('operation-1')
    assert anchor.goal_uuid == core.observe(
        'operation-1'
    ).current_goal_uuid
    assert anchor.terminal_event_digest == core.events(
        'operation-1'
    )[-1].event_digest
    assert 'x_mm' not in repr(anchor)
    assert 'y_mm' not in repr(anchor)


def test_registration_is_idempotent_and_survives_restart(tmp_path):
    """The same exact source replays, while process restart keeps the case."""
    core, _path = _core_unknown(tmp_path)
    sidecar_path = tmp_path / 'reconcile.sqlite3'
    sidecar = _sidecar(tmp_path, core)
    first = sidecar.register_unknown(core, 'operation-1', now=10.0)
    replay = sidecar.register_unknown(core, 'operation-1', now=10.0)
    assert replay.replayed is True
    assert replay.source_anchor_digest == first.source_anchor_digest
    sidecar.close()

    reopened = GazeboMonitorRoomNav2ReconcileStore(
        sidecar_path,
        core_store_namespace=core.store_namespace,
        quiescence_dwell_seconds=2.0,
        boot_id_reader=_boot_one,
    )
    assert reopened.observe('operation-1').source_anchor_digest == (
        first.source_anchor_digest
    )
    assert reopened.register_unknown(
        core, 'operation-1', now=10.0
    ).replayed is True


def test_registration_rejects_non_unknown_and_wrong_namespace(tmp_path):
    """Only an exact core unknown from the bound namespace can activate."""
    prepared = GazeboMonitorRoomStore(
        tmp_path / 'prepared.sqlite3', boot_id_reader=_boot_one
    )
    prepared.prepare(_request(), now=1.0)
    sidecar = _sidecar(tmp_path, prepared)
    with pytest.raises(GazeboMonitorRoomNav2ReconcileConflictError):
        sidecar.register_unknown(prepared, 'operation-1', now=2.0)

    core, _path = _core_unknown(tmp_path)
    wrong = GazeboMonitorRoomNav2ReconcileStore(
        tmp_path / 'wrong.sqlite3',
        core_store_namespace='f' * 32,
        boot_id_reader=_boot_one,
    )
    with pytest.raises(GazeboMonitorRoomNav2ReconcileConflictError):
        wrong.register_unknown(core, 'operation-1', now=10.0)


def test_independent_lease_renews_and_takes_over_with_new_fence(tmp_path):
    """Sidecar fencing is independent of the immutable core terminal fence."""
    _core, _path, sidecar, observation = _registered(tmp_path)
    assert observation.fence_epoch == 0
    first = _lease(sidecar)
    assert first.fence_epoch == 1
    assert first.taken_over is False
    renewed = _lease(sidecar, now=12.0, fence=1)
    assert renewed.fence_epoch == 1
    assert renewed.lease_expires_at == 32.0
    taken = _lease(
        sidecar,
        now=32.0,
        worker='replacement-worker',
        fence=1,
    )
    assert taken.fence_epoch == 2
    assert taken.taken_over is True
    with pytest.raises(GazeboMonitorRoomNav2ReconcileFenceError):
        _lease(sidecar, now=33.0, fence=1)


def test_lease_events_bind_expiry_authority_and_order(tmp_path):
    """Each append-only lease edge binds its full exact authority tuple."""
    _core, _path, sidecar, _observation = _registered(tmp_path)
    _lease(sidecar)
    _lease(sidecar, now=12.0, fence=1)
    _lease(
        sidecar,
        now=32.0,
        worker='replacement-worker',
        fence=1,
    )
    anchor = sidecar.source_anchor('operation-1')
    lease_events = tuple(
        event for event in sidecar.events('operation-1')
        if event.event_type.startswith('lease_')
    )
    assert [event.event_type for event in lease_events] == [
        'lease_acquired', 'lease_renewed', 'lease_taken_over'
    ]
    assert [event.lease_expires_at for event in lease_events] == [
        31.0, 32.0, 52.0
    ]
    for event in lease_events:
        assert event.evidence_digest == _lease_event_evidence(
            operation_id=event.operation_id,
            event_type=event.event_type,
            worker_id=event.worker_id,
            fence_epoch=event.fence_epoch,
            recorded_at=event.recorded_at,
            lease_expires_at=event.lease_expires_at,
            source_anchor_digest=anchor.anchor_digest,
        )


def test_lease_event_expiry_tamper_fails_startup_attestation(tmp_path):
    """Changing immutable lease expiry is detected after trigger restoration."""
    core, _path, sidecar, _observation = _registered(tmp_path)
    _lease(sidecar)
    path = tmp_path / 'reconcile.sqlite3'
    sidecar.close()
    connection = sqlite3.connect(path)
    connection.execute('DROP TRIGGER nav2_reconcile_event_no_update')
    connection.execute(
        '''
        UPDATE nav2_reconcile_events SET lease_expires_at = 99.0
        WHERE event_type = 'lease_acquired'
        '''
    )
    connection.execute(_EVENT_NO_UPDATE_SQL)
    connection.commit()
    connection.close()
    with pytest.raises(GazeboMonitorRoomNav2ReconcileSchemaError):
        GazeboMonitorRoomNav2ReconcileStore(
            path,
            core_store_namespace=core.store_namespace,
            quiescence_dwell_seconds=2.0,
            boot_id_reader=_boot_one,
        )


def test_materialized_lease_head_must_rejoin_append_only_history(tmp_path):
    """A self-consistent case-row forgery cannot detach from lease history."""
    core, _path, sidecar, _observation = _registered(tmp_path)
    _lease(sidecar)
    path = tmp_path / 'reconcile.sqlite3'
    sidecar.close()
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    values = dict(
        connection.execute(
            'SELECT * FROM nav2_reconcile_cases'
        ).fetchone()
    )
    values['lease_expires_at'] = 99.0
    values['row_digest'] = _case_digest(values)
    connection.execute(
        '''
        UPDATE nav2_reconcile_cases
        SET lease_expires_at = ?, row_digest = ?
        WHERE operation_id = ?
        ''',
        (
            values['lease_expires_at'],
            values['row_digest'],
            values['operation_id'],
        ),
    )
    connection.commit()
    connection.close()
    with pytest.raises(GazeboMonitorRoomNav2ReconcileSchemaError):
        GazeboMonitorRoomNav2ReconcileStore(
            path,
            core_store_namespace=core.store_namespace,
            quiescence_dwell_seconds=2.0,
            boot_id_reader=_boot_one,
        )


def test_active_lease_rejects_other_worker_and_clock_rollback(tmp_path):
    """A live owner is exclusive and durable BOOTTIME never moves backward."""
    _core, _path, sidecar, _observation = _registered(tmp_path)
    _lease(sidecar)
    with pytest.raises(GazeboMonitorRoomNav2ReconcileLeaseError):
        _lease(sidecar, now=12.0, worker='other-worker', fence=1)
    with pytest.raises(GazeboMonitorRoomNav2ReconcileClockRollbackError):
        sidecar.acquire_lease(
            'operation-1',
            worker_id='reconcile-worker',
            expected_fence=1,
            lease_seconds=20.0,
            now=9.0,
        )


def test_one_shot_attempt_claim_persists_before_result_and_restart(tmp_path):
    """A crash after claim cannot silently replay the same call identity."""
    core, _path, sidecar, _observation = _registered(tmp_path)
    lease = _lease(sidecar)
    first = _claim(sidecar, lease)
    assert first.claimed is True
    sidecar_path = tmp_path / 'reconcile.sqlite3'
    sidecar.close()

    reopened = GazeboMonitorRoomNav2ReconcileStore(
        sidecar_path,
        core_store_namespace=core.store_namespace,
        quiescence_dwell_seconds=2.0,
        boot_id_reader=_boot_one,
    )
    replay = reopened.claim_attempt(
        'operation-1',
        attempt_id='observe-1',
        kind='observe',
        worker_id='reconcile-worker',
        fence_epoch=1,
        request_fingerprint=_DIGEST_B,
        now=13.0,
    )
    assert replay.claimed is False
    assert replay.token == first.token
    assert len(reopened.attempts('operation-1')) == 1


def test_attempt_identity_conflict_and_cancel_wire_are_strict(tmp_path):
    """One ID cannot change payload and cancel claims require wire evidence."""
    _core, _path, sidecar, _observation = _registered(tmp_path)
    lease = _lease(sidecar)
    _claim(sidecar, lease)
    with pytest.raises(GazeboMonitorRoomNav2ReconcileConflictError):
        _claim(sidecar, lease, request=_DIGEST_C, now=13.0)
    with pytest.raises(GazeboMonitorRoomNav2ReconcileValidationError):
        _claim(
            sidecar,
            lease,
            attempt_id='cancel-1',
            kind='cancel',
            now=13.0,
        )
    cancel = _claim(
        sidecar,
        lease,
        attempt_id='cancel-1',
        kind='cancel',
        wire=_DIGEST_C,
        now=13.0,
    )
    assert cancel.claimed is True
    assert cancel.token.wire_payload_digest == _DIGEST_C


@pytest.mark.parametrize(
    'status', ['accepted', 'active', 'rejected', 'unknown']
)
def test_inconclusive_observation_never_proves_not_sent(tmp_path, status):
    """Standard Nav2 absence and nonterminal status remain blocked."""
    _core, _path, sidecar, _observation = _registered(tmp_path)
    lease = _lease(sidecar)
    claim = _claim(sidecar, lease)
    observed = sidecar.record_goal_observation(
        claim.token,
        status=status,
        evidence_digest=_DIGEST_C,
        now=13.0,
    )
    assert observed.state == 'blocked_unresolved'
    assert observed.terminal_goal_observed is False
    assert observed.robot_blocked is True


@pytest.mark.parametrize('status', ['succeeded', 'aborted', 'canceled'])
def test_exact_terminal_status_still_does_not_rewrite_operation(tmp_path, status):
    """Goal terminality is safety evidence, never retroactive core success."""
    core, _path, sidecar, _observation = _registered(tmp_path)
    lease = _lease(sidecar)
    observed = _terminal(sidecar, lease, status=status)
    assert observed.state == 'blocked_terminal_observed'
    assert observed.terminal_status == status
    assert observed.terminal_goal_observed is True
    assert observed.robot_blocked is True
    assert observed.operation_success is False
    assert core.observe('operation-1').state == 'delivery_unknown'


@pytest.mark.parametrize('status', ['active', 'rejected', 'unknown'])
def test_cancel_ack_without_canceled_result_remains_unresolved(
    tmp_path, status
):
    """Only exact STATUS_CANCELED provides terminal cancel evidence."""
    _core, _path, sidecar, _observation = _registered(
        tmp_path, source_state='cancel_unknown'
    )
    lease = _lease(sidecar)
    claim = _claim(
        sidecar,
        lease,
        attempt_id='cancel-1',
        kind='cancel',
        wire=_DIGEST_C,
    )
    observed = sidecar.record_cancel_observation(
        claim.token,
        status=status,
        evidence_digest=_DIGEST_B,
        now=13.0,
    )
    assert observed.state == 'blocked_unresolved'


def test_exact_canceled_cancel_report_is_terminal_but_still_blocked(tmp_path):
    """An exact canceled result advances only to terminal-observed."""
    _core, _path, sidecar, _observation = _registered(
        tmp_path, source_state='cancel_unknown'
    )
    lease = _lease(sidecar)
    claim = _claim(
        sidecar,
        lease,
        attempt_id='cancel-1',
        kind='cancel',
        wire=_DIGEST_C,
    )
    observed = sidecar.record_cancel_observation(
        claim.token,
        status='canceled',
        evidence_digest=_DIGEST_B,
        now=13.0,
    )
    assert observed.state == 'blocked_terminal_observed'
    assert observed.terminal_status == 'canceled'
    with pytest.raises(GazeboMonitorRoomNav2ReconcileConflictError):
        _claim(
            sidecar,
            lease,
            attempt_id='cancel-2',
            kind='cancel',
            wire=_DIGEST_C,
            now=14.0,
        )


def test_post_call_expiry_discards_evidence_but_keeps_attempt(tmp_path):
    """A result after the claimed lease edge cannot advance durable state."""
    _core, _path, sidecar, _observation = _registered(tmp_path)
    lease = sidecar.acquire_lease(
        'operation-1',
        worker_id='reconcile-worker',
        expected_fence=0,
        lease_seconds=2.0,
        now=11.0,
    )
    claim = _claim(sidecar, lease, now=12.0)
    with pytest.raises(GazeboMonitorRoomNav2ReconcileLeaseError):
        sidecar.record_goal_observation(
            claim.token,
            status='succeeded',
            evidence_digest=_DIGEST_C,
            now=13.0,
        )
    assert sidecar.observe('operation-1').state == 'blocked_unresolved'
    assert len(sidecar.attempts('operation-1')) == 1


def test_conflicting_terminal_evidence_fails_closed(tmp_path):
    """A second inconsistent terminal result permanently retains blocking."""
    _core, _path, sidecar, _observation = _registered(tmp_path)
    lease = _lease(sidecar)
    _terminal(sidecar, lease)
    second = _claim(
        sidecar,
        lease,
        attempt_id='observe-2',
        request=_DIGEST_C,
        now=14.0,
    )
    conflicted = sidecar.record_goal_observation(
        second.token,
        status='aborted',
        evidence_digest=_DIGEST_B,
        now=15.0,
    )
    assert conflicted.state == 'blocked_conflict'
    assert conflicted.robot_blocked is True
    with pytest.raises(GazeboMonitorRoomNav2ReconcileConflictError):
        _lease(sidecar, now=16.0, fence=1)


def test_quiescence_requires_terminal_and_dwell_then_mints_full_drop(tmp_path):
    """Only terminal plus later trusted quiescence mints a safety release."""
    core, _path, sidecar, _observation = _registered(tmp_path)
    lease = _lease(sidecar)
    with pytest.raises(GazeboMonitorRoomNav2ReconcileConflictError):
        _claim(
            sidecar,
            lease,
            attempt_id='quiet-too-early',
            kind='quiescence',
        )
    _terminal(sidecar, lease, now=13.0)
    quiet = _claim(
        sidecar,
        lease,
        attempt_id='quiet-1',
        kind='quiescence',
        request=_DIGEST_C,
        now=14.0,
    )
    with pytest.raises(GazeboMonitorRoomNav2ReconcileConflictError):
        sidecar.record_quiescence(
            quiet.token, evidence_digest=_DIGEST_B, now=14.9
        )
    released = sidecar.record_quiescence(
        quiet.token, evidence_digest=_DIGEST_B, now=15.0
    )
    assert released.state == 'released_quiescent'
    assert released.safe_block_released is True
    assert released.robot_blocked is False
    assert released.full_drop_certificate_digest is not None
    assert released.operation_success is False
    assert released.core_admission_released is False
    assert core.observe('operation-1').state == 'delivery_unknown'
    assert core.observe('operation-1').robot_blocked is True


def test_released_certificate_is_immutable_across_restart(tmp_path):
    """Restart preserves full-drop evidence and permits no new attempt."""
    core, _path, sidecar, _observation = _registered(tmp_path, dwell=0.0)
    lease = _lease(sidecar)
    _terminal(sidecar, lease)
    quiet = _claim(
        sidecar,
        lease,
        attempt_id='quiet-1',
        kind='quiescence',
        request=_DIGEST_C,
        now=14.0,
    )
    released = sidecar.record_quiescence(
        quiet.token, evidence_digest=_DIGEST_B, now=14.0
    )
    certificate = released.full_drop_certificate_digest
    path = tmp_path / 'reconcile.sqlite3'
    sidecar.close()
    reopened = GazeboMonitorRoomNav2ReconcileStore(
        path,
        core_store_namespace=core.store_namespace,
        quiescence_dwell_seconds=0.0,
        boot_id_reader=_boot_one,
    )
    assert reopened.observe(
        'operation-1'
    ).full_drop_certificate_digest == certificate
    with pytest.raises(GazeboMonitorRoomNav2ReconcileConflictError):
        reopened.acquire_lease(
            'operation-1',
            worker_id='reconcile-worker',
            expected_fence=1,
            lease_seconds=20.0,
            now=15.0,
        )


def test_attempts_and_events_are_sql_immutable_and_hash_attested(tmp_path):
    """Direct mutation is blocked and schema additions fail exact reopening."""
    core, _path, sidecar, _observation = _registered(tmp_path)
    lease = _lease(sidecar)
    _claim(sidecar, lease)
    path = tmp_path / 'reconcile.sqlite3'
    sidecar.close()
    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE nav2_reconcile_attempts SET kind = 'cancel'"
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute('DELETE FROM nav2_reconcile_events')
    connection.rollback()
    connection.execute('CREATE TABLE injected_private_data (value TEXT)')
    connection.commit()
    connection.close()
    with pytest.raises(GazeboMonitorRoomNav2ReconcileSchemaError):
        GazeboMonitorRoomNav2ReconcileStore(
            path,
            core_store_namespace=core.store_namespace,
            quiescence_dwell_seconds=2.0,
            boot_id_reader=_boot_one,
        )


def test_boot_identity_and_configuration_are_restart_bound(tmp_path):
    """A reboot identity or dwell-policy mismatch fails closed."""
    core, _path, sidecar, _observation = _registered(tmp_path)
    path = tmp_path / 'reconcile.sqlite3'
    sidecar.close()
    with pytest.raises(GazeboMonitorRoomNav2ReconcileSchemaError):
        GazeboMonitorRoomNav2ReconcileStore(
            path,
            core_store_namespace=core.store_namespace,
            quiescence_dwell_seconds=3.0,
            boot_id_reader=_boot_one,
        )
    with pytest.raises(GazeboMonitorRoomNav2ReconcileSchemaError):
        GazeboMonitorRoomNav2ReconcileStore(
            path,
            core_store_namespace=core.store_namespace,
            quiescence_dwell_seconds=2.0,
            boot_id_reader=_boot_two,
        )


def test_full_drop_digest_binds_quiescence_evidence(tmp_path):
    """Different trusted quiet evidence changes the full-drop certificate."""
    certificates = []
    for suffix, evidence in (('one', _DIGEST_A), ('two', _DIGEST_B)):
        scoped = tmp_path / suffix
        scoped.mkdir(mode=0o700)
        _core, _path, sidecar, _observation = _registered(
            scoped, dwell=0.0
        )
        lease = _lease(sidecar)
        _terminal(sidecar, lease)
        quiet = _claim(
            sidecar,
            lease,
            attempt_id='quiet-1',
            kind='quiescence',
            request=_DIGEST_C,
            now=14.0,
        )
        certificates.append(
            sidecar.record_quiescence(
                quiet.token, evidence_digest=evidence, now=14.0
            ).full_drop_certificate_digest
        )
    assert certificates[0] != certificates[1]
    assert all(
        len(value) == hashlib.sha256().digest_size * 2
        for value in certificates
    )
