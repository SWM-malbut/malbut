"""Adversarial tests for the durable simulation room-mission ledger."""

import hashlib
import os
import sqlite3
import stat
import threading
import time
from dataclasses import replace

import pytest

import malbut_agent_server.room_mission_ledger as ledger_module
from malbut_agent_server.room_mission_ledger import (
    ABORT_EXECUTION_CODES,
    CancelIntent,
    CancellationRequest,
    DurableMissionAuthority,
    DurableMissionConfirmation,
    DurableMissionProposal,
    FeedbackLease,
    MAX_AUTHORIZATION_TTL_SECONDS,
    PROPOSAL_INVALIDATION_CODES,
    RecoveryPhaseIntent,
    RECONCILIATION_FAILURE_CODE,
    ROOM_MISSION_SCHEMA_VERSION,
    ROOM_MISSION_WRITER_PROTOCOL_VERSION,
    RoomMissionLedgerAuthorityError,
    RoomMissionLedgerBusyError,
    RoomMissionLedgerCapacityError,
    RoomMissionLedgerClockError,
    RoomMissionLedgerConflictError,
    RoomMissionLedgerError,
    RoomMissionLedgerSchemaError,
    RoomMissionLedgerStateError,
    RoomMissionLedgerValidationError,
    SQLiteRoomMissionStore,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _operation_id(tool_call_id: str, phase: str) -> str:
    value = f'room-mission-operation/v1|{tool_call_id}|{phase}'
    return f'room-operation-{_digest(value)}'


def _feedback_id(tool_call_id: str) -> str:
    return f'room-feedback-{_digest(tool_call_id)}'


def _writer_connection(database):
    connection = sqlite3.connect(database)
    connection.create_function(
        'room_mission_writer_protocol_version',
        0,
        lambda: ROOM_MISSION_WRITER_PROTOCOL_VERSION,
    )
    return connection


class _Clock:
    """Mutable deterministic wall clock."""

    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _authority(
    *,
    subject_id: str = 'owner-1',
    auth_session_id: str = 'auth-session-1',
    conversation_id: str = 'conversation-1',
    conversation_instance: str = 'conversation-instance-1',
    turn_id: str = 'turn-1',
    request_id: str = 'request-1',
    revision: int = 1,
) -> DurableMissionAuthority:
    return DurableMissionAuthority(
        subject_id=subject_id,
        auth_session_id=auth_session_id,
        conversation_id=conversation_id,
        conversation_session_instance_id=conversation_instance,
        proposal_turn_id=turn_id,
        request_id=request_id,
        conversation_generation=1,
        conversation_revision=revision,
        conversation_ordinal=revision,
        authority_digest=_digest(f'authority-{request_id}'),
    )


def _proposal(
    authority=None,
    *,
    decision_id: str = 'decision-1',
    device_id: str = 'simulation-device-1',
    issued_at: float = 1000.0,
    expires_at: float = 1008.0,
) -> DurableMissionProposal:
    authority = authority or _authority()
    return DurableMissionProposal(
        authority=authority,
        decision_id=decision_id,
        arguments_digest=_digest('arguments-living-room'),
        device_id=device_id,
        device_binding_digest=_digest(f'device-binding-{device_id}'),
        map_id='home-a',
        map_revision=_digest('map-a'),
        room_id='room-living',
        plan_digest=_digest('approved-coverage-plan'),
        issued_at=issued_at,
        expires_at=expires_at,
    )


def _confirmation(
    proposal: DurableMissionProposal,
    *,
    confirmation_id: str = 'confirmation-1',
    issued_at: float = 1000.5,
    expires_at: float = 1007.0,
) -> DurableMissionConfirmation:
    return DurableMissionConfirmation(
        confirmation_id=confirmation_id,
        authority=proposal.authority,
        decision_id=proposal.decision_id,
        arguments_digest=proposal.arguments_digest,
        evidence_digest=_digest(f'evidence-{confirmation_id}'),
        issuer_id='trusted-confirmation-service',
        person_subject_id=proposal.authority.subject_id,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def _authorized(store, proposal=None, confirmation_id='confirmation-1'):
    proposal = proposal or _proposal()
    stored = store.register_proposal(proposal)
    confirmation = _confirmation(
        proposal,
        confirmation_id=confirmation_id,
        expires_at=min(1007.0, proposal.expires_at),
    )
    authorized = store.consume_confirmation(
        stored.proposal_id,
        proposal.authority,
        confirmation,
    )
    assert authorized.tool_call_id is not None
    return proposal, stored, confirmation, authorized


def _finish_success(store, authority, tool_call_id):
    lease = store.claim_execution(
        tool_call_id,
        authority,
        'simulation-worker-1',
    )
    observed = None
    for phase in ('preflight', 'navigating', 'coverage', 'live_ready'):
        intent = store.prepare_phase(lease, phase)
        observed = store.record_phase_result(
            lease,
            intent,
            'succeeded',
        )
    assert observed is not None
    return observed


def test_exact_proposal_replay_survives_restart(tmp_path) -> None:
    """An exact decision retry returns one durable opaque proposal."""
    database = tmp_path / 'room-ledger.sqlite3'
    clock = _Clock()
    proposal = _proposal()
    first = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        created = first.register_proposal(proposal)
        replay = first.register_proposal(proposal)
        assert created.cached is False
        assert replay == replace(created, cached=True)
    finally:
        first.close()

    reopened = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        replay = reopened.register_proposal(proposal)
        assert replay.proposal_id == created.proposal_id
        assert replay.status == 'proposed'
        assert replay.cached is True
    finally:
        reopened.close()


def test_store_identity_markers_are_truthful_and_immutable(tmp_path) -> None:
    """Persistence and lease scopes cannot be rewritten after opening."""
    database = tmp_path / 'identity.sqlite3'
    stores = (
        (
            SQLiteRoomMissionStore(':memory:', clock=_Clock()),
            ':memory:',
            'process_local',
            'store_connection',
        ),
        (
            SQLiteRoomMissionStore(str(database), clock=_Clock()),
            str(database),
            'sqlite_local',
            'database_device',
        ),
    )
    try:
        for store, path, durability, lease_scope in stores:
            assert store.database_path == path
            assert store.durability == durability
            assert store.lease_scope == lease_scope
            store.assert_durable_identity()
            with pytest.raises(AttributeError):
                store._IMMUTABLE_IDENTITY_FIELDS = frozenset()
            for name, forged in (
                ('database_path', '/tmp/forged-ledger.sqlite3'),
                ('_durability', 'sqlite_local'),
                ('_lease_scope', 'database_device'),
                ('durability', 'sqlite_local'),
                ('lease_scope', 'database_device'),
            ):
                with pytest.raises(AttributeError):
                    setattr(store, name, forged)
            for name in (
                '_connection', '_attested_connection',
                '_configured_database_path',
                '_attested_main_path', '_attested_file_device',
                '_attested_file_inode', '_closed', '_lock',
            ):
                with pytest.raises(AttributeError):
                    setattr(store, name, getattr(store, name))
            for name, forged in (
                ('assert_durable_identity', lambda: None),
                ('_durable_identity_matches_locked', lambda: True),
                ('__class__', type(store)),
                ('__dict__', {}),
            ):
                with pytest.raises(AttributeError):
                    setattr(store, name, forged)
            for name in (
                'database_path', '_durability', '_lease_scope',
                '_configured_database_path',
                '_connection', '_attested_connection',
                '_attested_main_path', '_attested_file_device',
                '_attested_file_inode', '_closed', '_lock',
                '_IMMUTABLE_IDENTITY_FIELDS',
                'assert_durable_identity',
                '_durable_identity_matches_locked',
                '__class__', '__dict__',
            ):
                with pytest.raises(AttributeError):
                    delattr(store, name)
            assert store.database_path == path
            assert store.durability == durability
            assert store.lease_scope == lease_scope

            proposal, _stored, _confirmation_value, authorized = (
                _authorized(store)
            )
            execution = store.get_execution(
                authorized.tool_call_id, proposal.authority
            )
            assert execution.durability == durability
            assert execution.lease_scope == lease_scope
    finally:
        for store, _path, _durability, _lease_scope in stores:
            store.close()


def test_connection_transplant_cannot_fake_file_durability(tmp_path) -> None:
    """The attested main DB rejects connection replacement and closure."""
    database = tmp_path / 'connection-identity.sqlite3'
    clock = _Clock()
    proposal = _proposal()
    durable = SQLiteRoomMissionStore(str(database), clock=clock)
    process_local = SQLiteRoomMissionStore(':memory:', clock=clock)
    original_connection = durable._connection
    try:
        created = durable.register_proposal(proposal)
        with pytest.raises(AttributeError):
            durable._connection = process_local._connection
        with pytest.raises(AttributeError):
            del durable._connection
        durable.assert_durable_identity()

        SQLiteRoomMissionStore._IMMUTABLE_IDENTITY_FIELDS = frozenset()
        try:
            with pytest.raises(AttributeError):
                durable._connection = process_local._connection
            with pytest.raises(AttributeError):
                durable.database_path = ':memory:'
            with pytest.raises(AttributeError):
                durable._durability = 'process_local'
            with pytest.raises(AttributeError):
                durable.assert_durable_identity = lambda: None
            with pytest.raises(AttributeError):
                durable._durable_identity_matches_locked = lambda: True
        finally:
            del SQLiteRoomMissionStore._IMMUTABLE_IDENTITY_FIELDS
        durable.assert_durable_identity()
        with pytest.raises(AttributeError):
            del durable._connection

        durable.__dict__['assert_durable_identity'] = (
            lambda: 'BYPASSED'
        )
        durable.__dict__['_durable_identity_matches_locked'] = (
            lambda: True
        )
        durable.__dict__['_assert_durable_identity_impl'] = (
            lambda: None
        )
        assert durable.assert_durable_identity() is None
        durable.__dict__['_connection'] = process_local._connection
        with pytest.raises(RoomMissionLedgerError) as transplanted:
            durable.assert_durable_identity()
        assert transplanted.value.__cause__ is None
        assert transplanted.value.__context__ is None
        assert str(database) not in str(transplanted.value)
        durable.__dict__['_connection'] = original_connection
        durable.__dict__.pop('assert_durable_identity')
        durable.__dict__.pop('_durable_identity_matches_locked')
        durable.__dict__.pop('_assert_durable_identity_impl')
        durable.assert_durable_identity()

        original_connection.close()
        with pytest.raises(AttributeError):
            durable._connection = process_local._connection
        with pytest.raises(RoomMissionLedgerError) as closed:
            durable.assert_durable_identity()
        assert closed.value.__cause__ is None
        assert closed.value.__context__ is None
        assert str(database) not in str(closed.value)
        with pytest.raises(RoomMissionLedgerError) as write_failure:
            durable.register_proposal(
                _proposal(decision_id='must-not-reach-memory')
            )
        assert write_failure.value.__cause__ is None
        assert write_failure.value.__context__ is None
        assert str(database) not in str(write_failure.value)
    finally:
        durable.close()
        process_local.close()

    reopened = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        reopened.assert_durable_identity()
        assert reopened.register_proposal(proposal) == replace(
            created, cached=True
        )
        assert reopened._connection.execute(
            'SELECT COUNT(*) FROM room_mission_proposals'
        ).fetchone()[0] == 1
        assert reopened._connection.execute(
            'SELECT COUNT(*) FROM room_mission_proposals '
            "WHERE decision_id = 'must-not-reach-memory'"
        ).fetchone()[0] == 0
    finally:
        reopened.close()


def test_coherent_file_identity_transplant_is_externally_fenced(
    tmp_path,
) -> None:
    """Replacing every visible identity field cannot redirect one store."""
    first_database = tmp_path / 'identity-first.sqlite3'
    second_database = tmp_path / 'identity-second.sqlite3'
    clock = _Clock()
    first = SQLiteRoomMissionStore(str(first_database), clock=clock)
    second = SQLiteRoomMissionStore(str(second_database), clock=clock)
    second_proposal = _proposal(decision_id='decision-second-original')
    second_created = second.register_proposal(second_proposal)
    try:
        original_connection = first._connection
        object.__setattr__(first, '_connection', second._connection)
        with pytest.raises(RoomMissionLedgerError):
            first.assert_durable_identity()
        object.__setattr__(first, '_connection', original_connection)
        first.assert_durable_identity()
        first.__dict__.update({
            name: getattr(second, name)
            for name in (
                'database_path',
                '_configured_database_path',
                '_connection',
                '_attested_connection',
                '_attested_main_path',
                '_attested_file_device',
                '_attested_file_inode',
                '_durability',
                '_lease_scope',
                '_closed',
            )
        })
        with pytest.raises(RoomMissionLedgerError) as identity_error:
            first.assert_durable_identity()
        with pytest.raises(RoomMissionLedgerError) as write_error:
            first.register_proposal(
                _proposal(decision_id='must-not-cross-databases')
            )
        for caught in (identity_error, write_error):
            assert caught.value.__cause__ is None
            assert caught.value.__context__ is None
            assert str(first_database) not in str(caught.value)
            assert str(second_database) not in str(caught.value)
        second.assert_durable_identity()
    finally:
        first.close()
        second.close()

    first_reopened = SQLiteRoomMissionStore(
        str(first_database), clock=clock
    )
    second_reopened = SQLiteRoomMissionStore(
        str(second_database), clock=clock
    )
    try:
        assert first_reopened._connection.execute(
            'SELECT COUNT(*) FROM room_mission_proposals'
        ).fetchone()[0] == 0
        assert second_reopened.register_proposal(
            second_proposal
        ) == replace(second_created, cached=True)
        assert second_reopened._connection.execute(
            'SELECT COUNT(*) FROM room_mission_proposals '
            "WHERE decision_id = 'must-not-cross-databases'"
        ).fetchone()[0] == 0
    finally:
        first_reopened.close()
        second_reopened.close()


def test_store_lock_cannot_be_shadowed_and_serializes_32_threads(
    tmp_path,
) -> None:
    """Slot-backed identity prevents lock shadows before concurrency."""
    database = tmp_path / 'lock-identity.sqlite3'
    store = SQLiteRoomMissionStore(str(database), clock=_Clock())
    barrier = threading.Barrier(32)
    results = []
    errors = []
    result_lock = threading.Lock()

    def register(index):
        proposal = _proposal(
            _authority(
                turn_id=f'turn-lock-{index}',
                request_id=f'request-lock-{index}',
                revision=index + 1,
            ),
            decision_id=f'decision-lock-{index}',
        )
        try:
            barrier.wait()
            result = store.register_proposal(proposal)
            with result_lock:
                results.append(result)
        except Exception as error:
            with result_lock:
                errors.append(error)

    threads = [
        threading.Thread(target=register, args=(index,))
        for index in range(32)
    ]
    try:
        original_lock = store._lock
        store.__dict__['_lock'] = object()
        assert store._lock is original_lock
        assert store.__dict__['_lock'] is not original_lock
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert len(results) == 32
        assert len({result.proposal_id for result in results}) == 32
        assert store._connection.execute(
            'SELECT COUNT(*) FROM room_mission_proposals'
        ).fetchone()[0] == 32
        del store.__dict__['_lock']
        assert store._lock is original_lock
        store.assert_durable_identity()
    finally:
        store.close()


def test_custom_clock_wait_estimates_cannot_reorder_transactions() -> None:
    """Out-of-order pre-lock samples preserve one monotonic time floor."""
    store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    try:
        with store._lock:
            store._begin_locked()
            first = store._fresh_transaction_time(
                1000.0,
                time.monotonic() - 2.0,
            )
            store._advance_clock_locked(first)
            store._connection.commit()
        with store._lock:
            store._begin_locked()
            second = store._fresh_transaction_time(
                1000.0,
                time.monotonic(),
            )
            store._advance_clock_locked(second)
            store._connection.commit()
        with store._lock:
            store._begin_locked()
            third = store._fresh_transaction_time(
                1000.5,
                time.monotonic(),
            )
            store._advance_clock_locked(third)
            store._connection.commit()
        with store._lock:
            store._begin_locked()
            stale = store._fresh_transaction_time(
                998.0,
                time.monotonic() - 10.0,
            )
            store._advance_clock_locked(stale)
            store._connection.commit()
        assert first >= 1002.0
        assert second >= first
        assert third >= first + 0.5
        assert stale >= third
        with store._lock:
            store._begin_locked()
            with pytest.raises(RoomMissionLedgerClockError):
                store._fresh_transaction_time(
                    998.0,
                    time.monotonic(),
                )
            store._connection.rollback()
    finally:
        store.close()


def test_unlinked_database_never_returns_durable_results(tmp_path) -> None:
    """Reads and writes reject an attested main file lost at runtime."""
    database = tmp_path / 'unlinked-identity.sqlite3'
    clock = _Clock()
    store = SQLiteRoomMissionStore(str(database), clock=clock)
    proposal, _stored, _confirmation_value, authorized = _authorized(store)
    next_proposal = _proposal(
        _authority(
            turn_id='turn-after-unlink',
            request_id='request-after-unlink',
            revision=2,
        ),
        decision_id='decision-after-unlink',
    )
    try:
        os.unlink(database)
        operations = (
            store.assert_durable_identity,
            lambda: store.durability,
            lambda: store.lease_scope,
            lambda: store.get_execution(
                authorized.tool_call_id, proposal.authority
            ),
            lambda: store.claim_execution(
                authorized.tool_call_id,
                proposal.authority,
                'simulation-worker-after-unlink',
            ),
            lambda: store.register_proposal(next_proposal),
        )
        for operation in operations:
            with pytest.raises(RoomMissionLedgerError) as caught:
                operation()
            assert caught.value.__cause__ is None
            assert caught.value.__context__ is None
            assert str(database) not in str(caught.value)
    finally:
        store.close()
    assert not database.exists()

    reopened = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        reopened.assert_durable_identity()
        assert reopened._connection.execute(
            'SELECT COUNT(*) FROM room_mission_proposals'
        ).fetchone()[0] == 0
    finally:
        reopened.close()


@pytest.mark.parametrize(
    'drift',
    (
        'database_permissions',
        'parent_permissions',
        'wal_permissions',
        'synchronous',
        'foreign_keys',
        'journal_mode',
    ),
)
def test_durable_identity_rejects_runtime_policy_drift(
    tmp_path,
    drift,
) -> None:
    """Runtime permission and SQLite policy weakening fails closed."""
    private_directory = tmp_path / f'private-{drift}'
    private_directory.mkdir(mode=0o700)
    database = private_directory / 'identity.sqlite3'
    store = SQLiteRoomMissionStore(str(database), clock=_Clock())
    wal_path = database.with_name(database.name + '-wal')
    restore = None
    try:
        store.assert_durable_identity()
        if drift == 'database_permissions':
            os.chmod(database, 0o666)
            restore = 'database_permissions'
        elif drift == 'parent_permissions':
            os.chmod(private_directory, 0o777)
            restore = 'parent_permissions'
        elif drift == 'wal_permissions':
            assert wal_path.exists()
            os.chmod(wal_path, 0o666)
            restore = 'wal_permissions'
        elif drift == 'synchronous':
            store._connection.execute('PRAGMA synchronous=OFF')
            restore = 'synchronous'
        elif drift == 'foreign_keys':
            store._connection.execute('PRAGMA foreign_keys=OFF')
            restore = 'foreign_keys'
        else:
            changed = store._connection.execute(
                'PRAGMA journal_mode=DELETE'
            ).fetchone()
            assert str(changed[0]).lower() == 'delete'
            restore = 'journal_mode'

        with pytest.raises(RoomMissionLedgerError) as caught:
            store.assert_durable_identity()
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert str(database) not in str(caught.value)
        with pytest.raises(RoomMissionLedgerError):
            store.register_proposal(
                _proposal(decision_id=f'decision-{drift}')
            )
    finally:
        if restore == 'database_permissions':
            os.chmod(database, 0o600)
        elif restore == 'parent_permissions':
            os.chmod(private_directory, 0o700)
        elif restore == 'wal_permissions' and wal_path.exists():
            os.chmod(wal_path, 0o600)
        elif restore == 'synchronous':
            store._connection.execute('PRAGMA synchronous=FULL')
        elif restore == 'foreign_keys':
            store._connection.execute('PRAGMA foreign_keys=ON')
        elif restore == 'journal_mode':
            store._connection.execute('PRAGMA journal_mode=WAL')
        store.close()


@pytest.mark.parametrize('suffix', ('', '-wal', '-shm'))
def test_post_commit_permission_drift_is_rejected_not_repaired(
    tmp_path,
    monkeypatch,
    suffix,
) -> None:
    """A committed write never returns success after permission drift."""
    private_directory = tmp_path / f'post-commit-{suffix or "main"}'
    private_directory.mkdir(mode=0o700)
    database = private_directory / 'identity.sqlite3'
    store = SQLiteRoomMissionStore(str(database), clock=_Clock())
    target = str(database) + suffix
    proposal = _proposal(decision_id=f'decision-post-commit-{suffix}')
    original_secure = SQLiteRoomMissionStore._secure_file_permissions

    def drift_before_check(candidate_store, *, provision=False):
        if not provision:
            assert os.path.exists(target)
            os.chmod(target, 0o666)
        return original_secure(
            candidate_store,
            provision=provision,
        )

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                SQLiteRoomMissionStore,
                '_secure_file_permissions',
                drift_before_check,
            )
            with pytest.raises(RoomMissionLedgerError) as caught:
                store.register_proposal(proposal)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert str(database) not in str(caught.value)
        assert stat.S_IMODE(os.lstat(target).st_mode) == 0o666
        os.chmod(target, 0o600)
        replay = store.register_proposal(proposal)
        assert replay.cached is True
        assert store._connection.execute(
            'SELECT COUNT(*) FROM room_mission_proposals'
        ).fetchone()[0] == 1
    finally:
        if os.path.exists(target):
            os.chmod(target, 0o600)
        store.close()


def test_connection_probe_is_content_free_bounded_and_cleaned(
    tmp_path,
) -> None:
    """A stale opaque probe is cleaned and successful opens leave none."""
    database = tmp_path / 'connection-probe.sqlite3'
    first = SQLiteRoomMissionStore(str(database), clock=_Clock())
    try:
        assert first._connection.execute(
            'SELECT COUNT(*) '
            'FROM room_mission_connection_attestations'
        ).fetchone()[0] == 0
    finally:
        first.close()

    writer = _writer_connection(database)
    try:
        writer.execute(
            '''
            INSERT INTO room_mission_connection_attestations (
                probe_digest,
                created_at
            ) VALUES (?, ?)
            ''',
            ('a' * 64, 0.0),
        )
        writer.commit()
    finally:
        writer.close()

    reopened = SQLiteRoomMissionStore(str(database), clock=_Clock())
    try:
        reopened.assert_durable_identity()
        assert reopened._connection.execute(
            'SELECT COUNT(*) '
            'FROM room_mission_connection_attestations'
        ).fetchone()[0] == 0
    finally:
        reopened.close()


def test_connection_probe_rejects_swap_back_open(
    tmp_path,
    monkeypatch,
) -> None:
    """A swapped inode restored after sqlite open fails path binding."""
    attested_path = tmp_path / 'attested.sqlite3'
    substitute_path = tmp_path / 'substitute.sqlite3'
    for database in (attested_path, substitute_path):
        SQLiteRoomMissionStore(str(database), clock=_Clock()).close()
    parked_path = tmp_path / 'attested-parked.sqlite3'
    real_connect = sqlite3.connect
    swapped = False

    def swap_once(path, *args, **kwargs):
        nonlocal swapped
        if not swapped and str(path) == str(attested_path.resolve()):
            swapped = True
            os.replace(attested_path, parked_path)
            os.replace(substitute_path, attested_path)
            try:
                connection = real_connect(path, *args, **kwargs)
            finally:
                os.replace(attested_path, substitute_path)
                os.replace(parked_path, attested_path)
            return connection
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(ledger_module.sqlite3, 'connect', swap_once)
    with pytest.raises(RoomMissionLedgerSchemaError) as caught:
        SQLiteRoomMissionStore(str(attested_path), clock=_Clock())
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert str(attested_path) not in str(caught.value)
    assert swapped is True

    monkeypatch.setattr(ledger_module.sqlite3, 'connect', real_connect)
    for database in (attested_path, substitute_path):
        reopened = SQLiteRoomMissionStore(str(database), clock=_Clock())
        try:
            reopened.assert_durable_identity()
            assert reopened._connection.execute(
                'SELECT COUNT(*) '
                'FROM room_mission_connection_attestations'
            ).fetchone()[0] == 0
        finally:
            reopened.close()


def test_changed_and_cross_owner_proposal_replay_fail_closed() -> None:
    """Owner mismatch hides the row and same-owner mutation conflicts."""
    store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    try:
        proposal = _proposal()
        store.register_proposal(proposal)
        with pytest.raises(RoomMissionLedgerConflictError):
            store.register_proposal(
                replace(proposal, plan_digest=_digest('other-plan'))
            )
        other = replace(
            proposal,
            authority=replace(
                proposal.authority,
                conversation_session_instance_id='other-instance',
            ),
        )
        with pytest.raises(RoomMissionLedgerAuthorityError):
            store.register_proposal(other)
    finally:
        store.close()


def test_confirmation_and_execution_are_atomic_and_restart_safe(
    tmp_path,
) -> None:
    """Confirmation replay returns one stable Tool ID after reopen."""
    database = tmp_path / 'atomic.sqlite3'
    clock = _Clock()
    proposal = _proposal()
    confirmation = _confirmation(proposal)
    first = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        stored = first.register_proposal(proposal)
        authorized = first.consume_confirmation(
            stored.proposal_id,
            proposal.authority,
            confirmation,
        )
        replay = first.consume_confirmation(
            stored.proposal_id,
            proposal.authority,
            confirmation,
        )
        assert authorized.cached is False
        assert replay == replace(authorized, cached=True)
    finally:
        first.close()

    reopened = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        replay = reopened.consume_confirmation(
            stored.proposal_id,
            proposal.authority,
            confirmation,
        )
        assert replay.tool_call_id == authorized.tool_call_id
        assert replay.cached is True
    finally:
        reopened.close()

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            'SELECT COUNT(*) FROM room_mission_confirmations'
        ).fetchone()[0] == 1
        assert connection.execute(
            'SELECT COUNT(*) FROM room_mission_executions'
        ).fetchone()[0] == 1
        assert connection.execute(
            'SELECT COUNT(*) FROM room_mission_events'
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_two_connections_concurrently_commit_one_proposal_and_tool(
    tmp_path,
) -> None:
    """BEGIN IMMEDIATE serializes exact retries across connections."""
    database = tmp_path / 'concurrent.sqlite3'
    clock = _Clock()
    stores = [
        SQLiteRoomMissionStore(str(database), clock=clock)
        for _index in range(2)
    ]
    proposal = _proposal()
    barrier = threading.Barrier(2)
    proposals = []
    errors = []

    def register(index):
        try:
            barrier.wait()
            proposals.append(stores[index].register_proposal(proposal))
        except Exception as error:  # evidence retains the actual exception
            errors.append(error)

    threads = [
        threading.Thread(target=register, args=(index,))
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert not errors, repr(errors)
    assert len({item.proposal_id for item in proposals}) == 1
    assert sorted(item.cached for item in proposals) == [False, True]

    confirmation = _confirmation(proposal)
    barrier = threading.Barrier(2)
    authorizations = []

    def confirm(index):
        try:
            barrier.wait()
            authorizations.append(stores[index].consume_confirmation(
                proposals[0].proposal_id,
                proposal.authority,
                confirmation,
            ))
        except Exception as error:  # evidence retains the actual exception
            errors.append(error)

    threads = [
        threading.Thread(target=confirm, args=(index,))
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    try:
        assert all(not thread.is_alive() for thread in threads)
        assert not errors, repr(errors)
        assert len({item.tool_call_id for item in authorizations}) == 1
        assert sorted(item.cached for item in authorizations) == [
            False,
            True,
        ]
    finally:
        for store in stores:
            store.close()


def test_cross_owner_confirmation_never_discloses_tool_id() -> None:
    """Reconstructed authority from another session cannot replay."""
    store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    try:
        proposal, stored, confirmation, authorized = _authorized(store)
        other = replace(
            proposal.authority,
            conversation_session_instance_id='other-instance',
        )
        forged = replace(confirmation, authority=other)
        with pytest.raises(RoomMissionLedgerAuthorityError) as caught:
            store.consume_confirmation(
                stored.proposal_id,
                other,
                forged,
            )
        assert authorized.tool_call_id not in str(caught.value)
        with pytest.raises(RoomMissionLedgerAuthorityError):
            store.get_execution(authorized.tool_call_id, other)
    finally:
        store.close()


def test_device_active_uniqueness_releases_only_after_terminal() -> None:
    """One database-wide device slot spans independent proposals."""
    store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    try:
        first, _stored, _confirmation_value, authorized = _authorized(store)
        second_authority = _authority(
            subject_id='owner-2',
            auth_session_id='auth-session-2',
            conversation_id='conversation-2',
            conversation_instance='conversation-instance-2',
            turn_id='turn-2',
            request_id='request-2',
            revision=2,
        )
        second = _proposal(
            second_authority,
            decision_id='decision-2',
        )
        second_stored = store.register_proposal(second)
        second_confirmation = _confirmation(
            second,
            confirmation_id='confirmation-2',
        )
        with pytest.raises(RoomMissionLedgerBusyError):
            store.consume_confirmation(
                second_stored.proposal_id,
                second.authority,
                second_confirmation,
            )

        terminal = _finish_success(
            store,
            first.authority,
            authorized.tool_call_id,
        )
        assert terminal.status == 'succeeded'
        second_authorized = store.consume_confirmation(
            second_stored.proposal_id,
            second.authority,
            second_confirmation,
        )
        assert second_authorized.status == 'pending'
    finally:
        store.close()


def test_claim_is_exclusive_and_stale_epoch_is_fenced(tmp_path) -> None:
    """A stale worker cannot write after a lease takeover."""
    database = tmp_path / 'lease.sqlite3'
    clock = _Clock()
    first = SQLiteRoomMissionStore(
        str(database),
        clock=clock,
        lease_seconds=1.0,
    )
    second = SQLiteRoomMissionStore(
        str(database),
        clock=clock,
        lease_seconds=1.0,
    )
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            first
        )
        initial = first.claim_execution(
            authorized.tool_call_id,
            proposal.authority,
            'worker-a',
        )
        with pytest.raises(RoomMissionLedgerBusyError):
            second.claim_execution(
                authorized.tool_call_id,
                proposal.authority,
                'worker-b',
            )
        clock.value += 1.1
        takeover = second.claim_execution(
            authorized.tool_call_id,
            proposal.authority,
            'worker-b',
        )
        assert takeover.lease_epoch == initial.lease_epoch + 1
        assert takeover.recovery_required is False
        with pytest.raises(RoomMissionLedgerBusyError):
            first.prepare_phase(initial, 'preflight')
        intent = second.prepare_phase(takeover, 'preflight')
        assert intent.phase == 'preflight'
    finally:
        first.close()
        second.close()


def test_stable_phase_intent_and_restart_recovery(tmp_path) -> None:
    """An unobserved intent survives restart and is never re-minted."""
    database = tmp_path / 'intent.sqlite3'
    clock = _Clock()
    first = SQLiteRoomMissionStore(
        str(database),
        clock=clock,
        lease_seconds=1.0,
    )
    proposal, _stored, _confirmation_value, authorized = _authorized(first)
    lease = first.claim_execution(
        authorized.tool_call_id,
        proposal.authority,
        'worker-a',
    )
    intent = first.prepare_phase(lease, 'preflight')
    first.close()

    clock.value += 1.1
    reopened = SQLiteRoomMissionStore(
        str(database),
        clock=clock,
        lease_seconds=1.0,
    )
    try:
        candidates = reopened.list_recovery_candidates(
            proposal.authority
        )
        assert len(candidates) == 1
        assert candidates[0].has_unresolved_intent is True
        recovered = reopened.claim_execution(
            authorized.tool_call_id,
            proposal.authority,
            'worker-recovery',
        )
        assert recovered.recovery_required is True
        with pytest.raises(RoomMissionLedgerStateError):
            reopened.prepare_phase(recovered, 'preflight')
        replay = reopened.get_recovery_intent(recovered)
        assert replay.operation_id == intent.operation_id
        assert reopened.list_events(
            authorized.tool_call_id,
            proposal.authority,
        )[-1].event_kind == 'recovery'
    finally:
        reopened.close()


def test_ordered_phases_terminalize_with_feedback_atomically(tmp_path) -> None:
    """Terminal execution, event, and feedback outbox share one commit."""
    database = tmp_path / 'terminal.sqlite3'
    clock = _Clock()
    store = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        terminal = _finish_success(
            store,
            proposal.authority,
            authorized.tool_call_id,
        )
        assert terminal.status == 'succeeded'
        assert terminal.phase == 'terminal'
        assert terminal.code == 'simulation_succeeded'
        assert terminal.viewer_live is False
        assert terminal.physical_effects is False
        assert terminal.terminal_digest is not None
        assert store.list_recovery_candidates(proposal.authority) == ()
    finally:
        store.close()

    reopened = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        replay = reopened.get_execution(
            authorized.tool_call_id,
            proposal.authority,
        )
        assert replay == terminal
        events = reopened.list_events(
            authorized.tool_call_id,
            proposal.authority,
        )
        assert [event.sequence for event in events] == list(
            range(1, len(events) + 1)
        )
        assert events[-1].event_kind == 'terminal'
        assert events[-1].source == 'simulation_adapter'
    finally:
        reopened.close()

    connection = sqlite3.connect(database)
    try:
        terminal_row = connection.execute(
            '''
            SELECT terminal_payload_json
            FROM room_mission_executions
            WHERE tool_call_id = ?
            ''',
            (authorized.tool_call_id,),
        ).fetchone()
        feedback_count = connection.execute(
            '''
            SELECT COUNT(*)
            FROM room_mission_feedback
            WHERE tool_call_id = ? AND state = 'pending'
            ''',
            (authorized.tool_call_id,),
        ).fetchone()[0]
        assert feedback_count == 1
        assert 'simulation_succeeded' in terminal_row[0]
        assert 'room-living' not in terminal_row[0]
    finally:
        connection.close()


def test_wrong_phase_and_changed_result_are_rejected() -> None:
    """The durable phase machine and result fingerprint fail closed."""
    store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        lease = store.claim_execution(
            authorized.tool_call_id,
            proposal.authority,
            'worker-a',
        )
        with pytest.raises(RoomMissionLedgerStateError):
            store.prepare_phase(lease, 'navigating')
        intent = store.prepare_phase(lease, 'preflight')
        first = store.record_phase_result(lease, intent, 'succeeded')
        replay = store.record_phase_result(lease, intent, 'succeeded')
        assert replay == first
        with pytest.raises(RoomMissionLedgerConflictError):
            store.record_phase_result(lease, intent, 'failed')
    finally:
        store.close()


def test_expiry_is_tombstoned_without_execution() -> None:
    """The exact expiry boundary consumes no confirmation or Tool ID."""
    clock = _Clock()
    store = SQLiteRoomMissionStore(':memory:', clock=clock)
    try:
        proposal = _proposal(expires_at=1002.0)
        stored = store.register_proposal(proposal)
        confirmation = _confirmation(
            proposal,
            expires_at=1002.0,
        )
        clock.value = 1002.0
        result = store.consume_confirmation(
            stored.proposal_id,
            proposal.authority,
            confirmation,
        )
        assert result.status == 'timed_out'
        assert result.tool_call_id is None
        assert store.list_recovery_candidates(proposal.authority) == ()
    finally:
        store.close()


def test_authorization_expiry_before_claim_is_durable_terminal(
    tmp_path,
) -> None:
    """A consumed but unstarted expired authorization never dispatches."""
    database = tmp_path / 'claim-expiry.sqlite3'
    clock = _Clock()
    proposal = _proposal(expires_at=1002.0)
    store = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        stored = store.register_proposal(proposal)
        confirmation = _confirmation(
            proposal,
            expires_at=1001.0,
        )
        authorized = store.consume_confirmation(
            stored.proposal_id,
            proposal.authority,
            confirmation,
        )
        clock.value = 1001.0
        with pytest.raises(RoomMissionLedgerStateError):
            store.claim_execution(
                authorized.tool_call_id,
                proposal.authority,
                'worker-a',
            )
        terminal = store.get_execution(
            authorized.tool_call_id,
            proposal.authority,
        )
        assert terminal.status == 'timed_out'
        assert terminal.code == 'authorization_expired'
        assert store.list_events(
            authorized.tool_call_id, proposal.authority
        )[-1].source == 'controller'
    finally:
        store.close()

    reopened = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        assert reopened.list_recovery_candidates(proposal.authority) == ()
    finally:
        reopened.close()


def test_capacity_fails_closed_but_preserves_exact_replay() -> None:
    """Terminal tombstones count toward the cap and are never evicted."""
    store = SQLiteRoomMissionStore(
        ':memory:',
        clock=_Clock(),
        max_mission_records=1,
    )
    try:
        first = _proposal()
        created = store.register_proposal(first)
        assert store.register_proposal(first) == replace(
            created,
            cached=True,
        )
        second_authority = _authority(
            request_id='request-2',
            turn_id='turn-2',
            revision=2,
        )
        second = _proposal(
            second_authority,
            decision_id='decision-2',
        )
        with pytest.raises(RoomMissionLedgerCapacityError):
            store.register_proposal(second)
        assert store.register_proposal(first).proposal_id == (
            created.proposal_id
        )
    finally:
        store.close()


def test_schema_version_and_unmanaged_writer_fail_closed(tmp_path) -> None:
    """Unknown schema writers cannot mutate or reopen the ledger."""
    database = tmp_path / 'schema.sqlite3'
    clock = _Clock()
    store = SQLiteRoomMissionStore(str(database), clock=clock)
    store.close()

    legacy = sqlite3.connect(database)
    legacy.create_function(
        'room_mission_writer_protocol_version',
        0,
        lambda: ROOM_MISSION_WRITER_PROTOCOL_VERSION + 1,
    )
    try:
        with pytest.raises(
            sqlite3.IntegrityError,
            match='incompatible room mission writer protocol',
        ):
            legacy.execute(
                '''
                UPDATE room_mission_store_state
                SET revision = revision + 1
                WHERE singleton = 1
                '''
            )
        legacy.rollback()
    finally:
        legacy.close()

    connection = sqlite3.connect(database)
    connection.create_function(
        'room_mission_writer_protocol_version',
        0,
        lambda: ROOM_MISSION_WRITER_PROTOCOL_VERSION,
    )
    try:
        connection.execute(
            '''
            UPDATE room_mission_schema_metadata
            SET schema_version = ?
            WHERE singleton = 1
            ''',
            (ROOM_MISSION_SCHEMA_VERSION + 1,),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RoomMissionLedgerSchemaError):
        SQLiteRoomMissionStore(str(database), clock=clock)


def test_concurrent_first_open_and_file_permissions(tmp_path) -> None:
    """First-open schema creation is serialized and files stay private."""
    database = tmp_path / 'first-open.sqlite3'
    clock = _Clock()
    barrier = threading.Barrier(8)
    errors = []

    def open_store():
        try:
            barrier.wait()
            store = SQLiteRoomMissionStore(str(database), clock=clock)
            store.close()
        except Exception as error:  # evidence retains the actual exception
            errors.append(error)

    threads = [threading.Thread(target=open_store) for _index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert not errors, repr(errors)
    assert os.stat(database).st_mode & 0o777 == 0o600

    store = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        connection = sqlite3.connect(database)
        try:
            metadata = connection.execute(
                '''
                SELECT schema_version,
                       min_writer_protocol,
                       max_writer_protocol
                FROM room_mission_schema_metadata
                WHERE singleton = 1
                '''
            ).fetchone()
            assert metadata == (
                ROOM_MISSION_SCHEMA_VERSION,
                ROOM_MISSION_WRITER_PROTOCOL_VERSION,
                ROOM_MISSION_WRITER_PROTOCOL_VERSION,
            )
        finally:
            connection.close()
    finally:
        store.close()


def test_clock_and_callback_errors_are_sanitized() -> None:
    """Dependency exceptions never retain secret causes or messages."""
    def broken_clock():
        raise RuntimeError('SECRET_DATABASE_PATH_AND_TOKEN')

    store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    store._clock = broken_clock
    try:
        with pytest.raises(RoomMissionLedgerClockError) as caught:
            store.register_proposal(_proposal())
        assert 'SECRET' not in str(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
    finally:
        store.close()


def test_authorization_ttl_never_exceeds_existing_ten_second_policy() -> None:
    """Standalone durable envelopes cannot widen consent lifetime."""
    assert MAX_AUTHORIZATION_TTL_SECONDS == 10.0
    _proposal(expires_at=1010.0)
    with pytest.raises(ValueError):
        _proposal(expires_at=1010.001)
    proposal = _proposal(expires_at=1010.0)
    _confirmation(proposal, issued_at=1000.0, expires_at=1010.0)
    with pytest.raises(ValueError):
        _confirmation(
            proposal,
            issued_at=999.999,
            expires_at=1010.0,
        )


def test_duplicate_intent_requires_typed_reconciliation() -> None:
    """A persisted intent receipt never authorizes blind redispatch."""
    store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        lease = store.claim_execution(
            authorized.tool_call_id,
            proposal.authority,
            'worker-a',
        )
        intent = store.prepare_phase(lease, 'preflight')
        with pytest.raises(RoomMissionLedgerStateError):
            store.prepare_phase(lease, 'preflight')
        recovery = store.get_recovery_intent(lease)
        assert isinstance(recovery, RecoveryPhaseIntent)
        assert recovery.operation_id == intent.operation_id
        observed = store.record_phase_result(
            lease, recovery, 'succeeded'
        )
        assert observed.code == 'preflight_succeeded'
        resumed = store.claim_execution(
            authorized.tool_call_id,
            proposal.authority,
            'worker-b',
        )
        assert resumed.recovery_required is False
        assert store.prepare_phase(resumed, 'navigating').phase == (
            'navigating'
        )
    finally:
        store.close()


def test_expired_recovery_lease_is_exclusive_and_cannot_redispatch() -> None:
    """Expired consent permits observation only, then durable timeout."""
    clock = _Clock()
    store = SQLiteRoomMissionStore(
        ':memory:', clock=clock, lease_seconds=0.5
    )
    try:
        proposal = _proposal(expires_at=1002.0)
        confirmation = _confirmation(proposal, expires_at=1001.0)
        stored = store.register_proposal(proposal)
        authorized = store.consume_confirmation(
            stored.proposal_id, proposal.authority, confirmation
        )
        lease = store.claim_execution(
            authorized.tool_call_id, proposal.authority, 'worker-a'
        )
        store.prepare_phase(lease, 'preflight')
        clock.value = 1001.1
        recovered = store.claim_execution(
            authorized.tool_call_id, proposal.authority, 'recovery-a'
        )
        assert recovered.recovery_required is True
        with pytest.raises(RoomMissionLedgerBusyError):
            store.claim_execution(
                authorized.tool_call_id,
                proposal.authority,
                'recovery-b',
            )
        renewed = store.renew_lease(recovered)
        recovery_intent = store.get_recovery_intent(renewed)
        fabricated = RecoveryPhaseIntent(
            tool_call_id=authorized.tool_call_id,
            phase='navigating',
            operation_id=_operation_id(
                authorized.tool_call_id, 'navigating'
            ),
            state_revision=recovery_intent.state_revision,
        )
        with pytest.raises(RoomMissionLedgerStateError):
            store.record_phase_result(renewed, fabricated, 'succeeded')
        terminal = store.record_phase_result(
            renewed, recovery_intent, 'succeeded'
        )
        assert terminal.status == 'timed_out'
        assert terminal.code == 'authorization_expired'
        assert store.list_events(
            authorized.tool_call_id, proposal.authority
        )[-1].source == 'recovery'
        assert store.record_phase_result(
            renewed, recovery_intent, 'succeeded'
        ) == terminal
        with pytest.raises(RoomMissionLedgerConflictError):
            store.record_phase_result(
                renewed, recovery_intent, 'failed'
            )
        with pytest.raises(
            (RoomMissionLedgerBusyError, RoomMissionLedgerStateError)
        ):
            store.prepare_phase(renewed, 'navigating')
    finally:
        store.close()


def test_exact_ids_survive_restart_without_id_generation(tmp_path) -> None:
    """Exact replay needs no new ID and authorization stays immutable."""
    database = tmp_path / 'receipt.sqlite3'
    clock = _Clock()
    proposal, stored, confirmation, authorized = (None,) * 4
    first = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        proposal, stored, confirmation, authorized = _authorized(first)
        lease = first.claim_execution(
            authorized.tool_call_id, proposal.authority, 'worker-a'
        )
        intent = first.prepare_phase(lease, 'preflight')
        first.record_phase_result(lease, intent, 'succeeded')
        replay = first.consume_confirmation(
            stored.proposal_id, proposal.authority, confirmation
        )
        assert replay == replace(authorized, cached=True)
    finally:
        first.close()

    def broken_id_factory():
        raise RuntimeError('SECRET-ID-FACTORY')

    reopened = SQLiteRoomMissionStore(
        str(database), clock=clock, id_factory=broken_id_factory
    )
    try:
        proposal_replay = reopened.register_proposal(proposal)
        assert proposal_replay.proposal_id == stored.proposal_id
        assert proposal_replay.cached is True
        replay = reopened.consume_confirmation(
            stored.proposal_id, proposal.authority, confirmation
        )
        assert replay == replace(authorized, cached=True)
    finally:
        reopened.close()


def test_expired_clean_execution_releases_device_slot() -> None:
    """An expired pending execution cannot block its device forever."""
    clock = _Clock()
    store = SQLiteRoomMissionStore(':memory:', clock=clock)
    try:
        first = _proposal(expires_at=1002.0)
        first_stored = store.register_proposal(first)
        first_auth = store.consume_confirmation(
            first_stored.proposal_id,
            first.authority,
            _confirmation(first, expires_at=1001.0),
        )
        second_authority = _authority(
            subject_id='owner-2',
            auth_session_id='auth-session-2',
            conversation_id='conversation-2',
            conversation_instance='instance-2',
            turn_id='turn-2',
            request_id='request-2',
            revision=2,
        )
        second = _proposal(
            second_authority,
            decision_id='decision-2',
        )
        second_stored = store.register_proposal(second)
        clock.value = 1001.0
        second_auth = store.consume_confirmation(
            second_stored.proposal_id,
            second.authority,
            _confirmation(second, confirmation_id='confirmation-2'),
        )
        assert second_auth.tool_call_id is not None
        expired = store.get_execution(
            first_auth.tool_call_id, first.authority
        )
        assert expired.status == 'timed_out'
    finally:
        store.close()


def test_owner_scoped_recovery_and_truthful_memory_markers() -> None:
    """Recovery discovery is owner-bound and memory is not durable."""
    store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        execution = store.get_execution(
            authorized.tool_call_id, proposal.authority
        )
        assert execution.durability == 'process_local'
        assert execution.lease_scope == 'store_connection'
        other = replace(
            proposal.authority,
            conversation_session_instance_id='another-instance',
        )
        assert store.list_recovery_candidates(other) == ()
    finally:
        store.close()


def test_record_capacity_configuration_is_persisted(tmp_path) -> None:
    """Two writers cannot silently disagree on the durable cap."""
    database = tmp_path / 'capacity.sqlite3'
    store = SQLiteRoomMissionStore(
        str(database), clock=_Clock(), max_mission_records=2
    )
    store.close()
    with pytest.raises(RoomMissionLedgerSchemaError):
        SQLiteRoomMissionStore(
            str(database), clock=_Clock(), max_mission_records=3
        )


def test_event_cap_reserves_terminal_slot_and_releases_device(
    monkeypatch,
) -> None:
    """Takeover churn cannot consume the final terminal event slot."""
    clock = _Clock()
    store = SQLiteRoomMissionStore(
        ':memory:', clock=clock, lease_seconds=0.05
    )
    try:
        first, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        monkeypatch.setattr(ledger_module, 'MAX_EVENTS_PER_MISSION', 6)
        for index in range(5):
            if index:
                clock.value += 0.051
            if index == 4:
                with pytest.raises(RoomMissionLedgerStateError):
                    store.claim_execution(
                        authorized.tool_call_id,
                        first.authority,
                        f'worker-{index}',
                    )
            else:
                store.claim_execution(
                    authorized.tool_call_id,
                    first.authority,
                    f'worker-{index}',
                )
        terminal = store.get_execution(
            authorized.tool_call_id, first.authority
        )
        assert terminal.status == 'failed'
        assert terminal.code == 'event_capacity_reached'
        events = store.list_events(
            authorized.tool_call_id, first.authority
        )
        assert len(events) == 6
        assert events[-1].event_kind == 'terminal'
        assert events[-1].source == 'controller'

        other_authority = _authority(
            subject_id='owner-2',
            auth_session_id='auth-session-2',
            conversation_id='conversation-2',
            conversation_instance='instance-2',
            turn_id='turn-2',
            request_id='request-2',
            revision=2,
        )
        other = _proposal(other_authority, decision_id='decision-2')
        other_stored = store.register_proposal(other)
        assert store.consume_confirmation(
            other_stored.proposal_id,
            other.authority,
            _confirmation(other, confirmation_id='confirmation-2'),
        ).tool_call_id is not None
    finally:
        store.close()


def test_writer_lock_wait_cannot_revive_expired_confirmation(tmp_path) -> None:
    """Deadline checks use a fresh commit-boundary timestamp."""
    database = tmp_path / 'confirmation-lock.sqlite3'
    now = time.time()
    proposal = _proposal(issued_at=now, expires_at=now + 2.0)
    confirmation = _confirmation(
        proposal,
        issued_at=now + 0.01,
        expires_at=now + 0.25,
    )
    store = SQLiteRoomMissionStore(str(database))
    blocker = sqlite3.connect(database)
    try:
        stored = store.register_proposal(proposal)
        blocker.execute('BEGIN IMMEDIATE')
        results = []
        errors = []

        def consume():
            try:
                results.append(store.consume_confirmation(
                    stored.proposal_id,
                    proposal.authority,
                    confirmation,
                ))
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=consume)
        thread.start()
        time.sleep(0.35)
        blocker.rollback()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not errors, repr(errors)
        assert results[0].status == 'timed_out'
        assert results[0].tool_call_id is None
    finally:
        blocker.close()
        store.close()


def test_writer_lock_wait_cannot_commit_after_lease_expiry(tmp_path) -> None:
    """A result blocked on a writer lock is fenced after lease expiry."""
    database = tmp_path / 'lease-lock.sqlite3'
    now = time.time()
    proposal = _proposal(issued_at=now, expires_at=now + 2.0)
    confirmation = _confirmation(
        proposal,
        issued_at=now + 0.01,
        expires_at=now + 2.0,
    )
    store = SQLiteRoomMissionStore(
        str(database), lease_seconds=0.05
    )
    blocker = sqlite3.connect(database)
    try:
        stored = store.register_proposal(proposal)
        authorized = store.consume_confirmation(
            stored.proposal_id, proposal.authority, confirmation
        )
        lease = store.claim_execution(
            authorized.tool_call_id, proposal.authority, 'worker-a'
        )
        intent = store.prepare_phase(lease, 'preflight')
        blocker.execute('BEGIN IMMEDIATE')
        errors = []

        def record():
            try:
                store.record_phase_result(lease, intent, 'succeeded')
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=record)
        thread.start()
        time.sleep(0.15)
        blocker.rollback()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], RoomMissionLedgerBusyError)
        current = store.get_execution(
            authorized.tool_call_id, proposal.authority
        )
        assert current.active_operation_id == intent.operation_id
    finally:
        blocker.close()
        store.close()


def test_raw_capabilities_are_never_persisted(tmp_path) -> None:
    """Only digests of session, evidence, and lease capabilities persist."""
    database = tmp_path / 'privacy.sqlite3'
    auth_secret = 'BEARER_SESSION_SECRET_NEVER_PERSIST'
    evidence_secret = 'RAW_CONFIRMATION_SECRET_NEVER_PERSIST'
    authority = _authority(auth_session_id=auth_secret)
    proposal = _proposal(authority)
    confirmation = replace(
        _confirmation(proposal),
        evidence_digest=_digest(evidence_secret),
    )
    store = SQLiteRoomMissionStore(str(database), clock=_Clock())
    lease_token = None
    try:
        stored = store.register_proposal(proposal)
        authorized = store.consume_confirmation(
            stored.proposal_id, proposal.authority, confirmation
        )
        lease = store.claim_execution(
            authorized.tool_call_id, proposal.authority, 'worker-a'
        )
        lease_token = lease.lease_token
        for suffix in ('', '-wal', '-shm'):
            path = str(database) + suffix
            if os.path.exists(path):
                assert os.stat(path).st_mode & 0o777 == 0o600
    finally:
        store.close()
    durable_bytes = b''.join(
        (tmp_path / name).read_bytes()
        for name in os.listdir(tmp_path)
        if name.startswith('privacy.sqlite3')
    )
    for secret in (auth_secret, evidence_secret, lease_token):
        assert secret.encode('utf-8') not in durable_bytes
    connection = sqlite3.connect(database)
    try:
        columns = {
            row[1]
            for row in connection.execute(
                'PRAGMA table_info(room_mission_proposals)'
            )
        }
        assert 'auth_session_digest' in columns
        assert 'auth_session_id' not in columns
    finally:
        connection.close()


def test_hardlink_and_world_writable_parent_fail_closed(tmp_path) -> None:
    """Database sidecars require one private owner-controlled target."""
    database = tmp_path / 'private.sqlite3'
    store = SQLiteRoomMissionStore(str(database), clock=_Clock())
    store.close()
    alias = tmp_path / 'alias.sqlite3'
    os.link(database, alias)
    with pytest.raises(RoomMissionLedgerSchemaError):
        SQLiteRoomMissionStore(str(database), clock=_Clock())

    unsafe = tmp_path / 'unsafe'
    unsafe.mkdir()
    os.chmod(unsafe, 0o777)
    with pytest.raises(RoomMissionLedgerSchemaError) as caught:
        SQLiteRoomMissionStore(
            str(unsafe / 'ledger.sqlite3'), clock=_Clock()
        )
    assert str(unsafe) not in str(caught.value)


@pytest.mark.parametrize(
    'mutation',
    (
        "UPDATE room_mission_proposals SET room_id = 'redirected'",
        "UPDATE room_mission_executions SET device_id = 'other-device'",
        "UPDATE room_mission_executions SET active_operation_id = 'fake'",
    ),
)
def test_logical_record_corruption_fails_reopen(tmp_path, mutation) -> None:
    """Digest, linkage, and active-intent corruption all fail closed."""
    database = tmp_path / 'corrupt.sqlite3'
    store = SQLiteRoomMissionStore(str(database), clock=_Clock())
    try:
        _authorized(store)
    finally:
        store.close()
    connection = _writer_connection(database)
    try:
        connection.execute(mutation)
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RoomMissionLedgerSchemaError):
        SQLiteRoomMissionStore(str(database), clock=_Clock())


def test_index_tamper_and_partial_schema_fail_closed(tmp_path) -> None:
    """Named impostor indexes and unmanaged partial schemas are rejected."""
    database = tmp_path / 'index.sqlite3'
    store = SQLiteRoomMissionStore(str(database), clock=_Clock())
    store.close()
    connection = sqlite3.connect(database)
    try:
        connection.execute('DROP INDEX room_mission_one_active_device_idx')
        connection.execute(
            'CREATE INDEX room_mission_one_active_device_idx '
            'ON room_mission_executions(tool_call_id)'
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RoomMissionLedgerSchemaError):
        SQLiteRoomMissionStore(str(database), clock=_Clock())

    partial = tmp_path / 'partial.sqlite3'
    connection = sqlite3.connect(partial)
    connection.execute('CREATE TABLE room_mission_store_state (x INTEGER)')
    connection.commit()
    connection.close()
    with pytest.raises(RoomMissionLedgerSchemaError):
        SQLiteRoomMissionStore(str(partial), clock=_Clock())


def test_subject_binding_and_closed_store_errors_are_private() -> None:
    """Caller-edited subject evidence and raw SQLite errors never escape."""
    store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    proposal = _proposal()
    stored = store.register_proposal(proposal)
    forged = replace(
        _confirmation(proposal), person_subject_id='another-person'
    )
    with pytest.raises(RoomMissionLedgerAuthorityError):
        store.consume_confirmation(
            stored.proposal_id, proposal.authority, forged
        )
    store.close()
    with pytest.raises(RoomMissionLedgerError) as caught:
        store.register_proposal(proposal)
    assert 'closed database' not in str(caught.value).lower()
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_expired_unresolved_claim_reserves_cap_terminal(monkeypatch) -> None:
    """A two-event expiry transition cannot consume the terminal slot."""
    clock = _Clock()
    store = SQLiteRoomMissionStore(
        ':memory:', clock=clock, lease_seconds=0.05
    )
    try:
        proposal = _proposal(expires_at=1002.0)
        stored = store.register_proposal(proposal)
        confirmation = _confirmation(proposal, expires_at=1001.0)
        authorized = store.consume_confirmation(
            stored.proposal_id, proposal.authority, confirmation
        )
        lease = store.claim_execution(
            authorized.tool_call_id, proposal.authority, 'worker-a'
        )
        store.prepare_phase(lease, 'preflight')
        monkeypatch.setattr(ledger_module, 'MAX_EVENTS_PER_MISSION', 5)
        clock.value = 1001.1
        with pytest.raises(RoomMissionLedgerStateError):
            store.claim_execution(
                authorized.tool_call_id,
                proposal.authority,
                'recovery-a',
            )
        terminal = store.get_execution(
            authorized.tool_call_id, proposal.authority
        )
        assert terminal.status == 'failed'
        assert terminal.code == 'event_capacity_reached'
        events = store.list_events(
            authorized.tool_call_id, proposal.authority
        )
        assert len(events) == 5
        assert events[-1].event_kind == 'terminal'
    finally:
        store.close()


@pytest.mark.parametrize(
    ('completed_phases', 'next_phase'),
    (
        (('preflight',), 'navigating'),
        (('preflight', 'navigating'), 'coverage'),
        (('preflight', 'navigating', 'coverage'), 'live_ready'),
    ),
)
def test_expired_lease_between_phases_resumes_without_recovery(
    completed_phases,
    next_phase,
) -> None:
    """A crash between completed phases preserves forward progress."""
    clock = _Clock()
    store = SQLiteRoomMissionStore(
        ':memory:', clock=clock, lease_seconds=0.05
    )
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        lease = store.claim_execution(
            authorized.tool_call_id, proposal.authority, 'worker-a'
        )
        for phase in completed_phases:
            intent = store.prepare_phase(lease, phase)
            store.record_phase_result(lease, intent, 'succeeded')
        clock.value += 0.051
        resumed = store.claim_execution(
            authorized.tool_call_id, proposal.authority, 'worker-b'
        )
        assert resumed.recovery_required is False
        assert store.prepare_phase(resumed, next_phase).phase == next_phase
    finally:
        store.close()


def test_timed_out_confirmation_exact_replay_survives_restart(
    tmp_path,
) -> None:
    """Expiry tombstones return the same content-free response on retry."""
    database = tmp_path / 'timeout-replay.sqlite3'
    clock = _Clock()
    proposal = _proposal(expires_at=1002.0)
    confirmation = _confirmation(proposal, expires_at=1001.0)
    store = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        stored = store.register_proposal(proposal)
        clock.value = 1001.0
        first = store.consume_confirmation(
            stored.proposal_id, proposal.authority, confirmation
        )
        assert first.status == 'timed_out'
    finally:
        store.close()
    reopened = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        replay = reopened.consume_confirmation(
            stored.proposal_id, proposal.authority, confirmation
        )
        assert replay == replace(first, cached=True)
    finally:
        reopened.close()


def test_terminal_result_exact_replay_requires_original_lease() -> None:
    """Lost terminal responses replay exactly but forged receipts fail."""
    store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        lease = store.claim_execution(
            authorized.tool_call_id, proposal.authority, 'worker-a'
        )
        final_intent = None
        for phase in ('preflight', 'navigating', 'coverage', 'live_ready'):
            intent = store.prepare_phase(lease, phase)
            if phase == 'live_ready':
                final_intent = intent
                break
            store.record_phase_result(lease, intent, 'succeeded')
        terminal = store.record_phase_result(
            lease, final_intent, 'succeeded'
        )
        assert store.record_phase_result(
            lease, final_intent, 'succeeded'
        ) == terminal
        with pytest.raises(RoomMissionLedgerConflictError):
            store.record_phase_result(lease, final_intent, 'failed')
        with pytest.raises(RoomMissionLedgerBusyError):
            store.record_phase_result(
                replace(lease, lease_token='x' * 32),
                final_intent,
                'succeeded',
            )
    finally:
        store.close()


@pytest.mark.parametrize(
    'target', ('proposal', 'feedback', 'feedback_ids')
)
def test_link_and_feedback_state_corruption_fail_reopen(
    tmp_path,
    target,
) -> None:
    """Latent handoff fields and proposal linkage remain unambiguous."""
    database = tmp_path / f'{target}.sqlite3'
    store = SQLiteRoomMissionStore(str(database), clock=_Clock())
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        if target in {'feedback', 'feedback_ids'}:
            _finish_success(
                store, proposal.authority, authorized.tool_call_id
            )
    finally:
        store.close()
    connection = _writer_connection(database)
    try:
        if target == 'proposal':
            connection.execute(
                "UPDATE room_mission_proposals SET status = 'proposed'"
            )
        elif target == 'feedback':
            connection.execute('PRAGMA ignore_check_constraints=ON')
            connection.execute(
                "UPDATE room_mission_feedback SET lease_owner = 'bad'"
            )
        else:
            connection.execute(
                "UPDATE room_mission_feedback "
                "SET feedback_request_id = 'redirected-request', "
                "feedback_turn_id = 'redirected-turn'"
            )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RoomMissionLedgerSchemaError):
        SQLiteRoomMissionStore(str(database), clock=_Clock())


@pytest.mark.parametrize('target', ('fake', 'missing', 'resolved'))
def test_cancel_target_corruption_fails_reopen(tmp_path, target) -> None:
    """The phase superseded by cancellation is an immutable binding."""
    database = tmp_path / f'cancel-target-{target}.sqlite3'
    store = SQLiteRoomMissionStore(str(database), clock=_Clock())
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        lease = store.claim_execution(
            authorized.tool_call_id, proposal.authority, 'worker-a'
        )
        phase_intent = store.prepare_phase(lease, 'preflight')
        if target == 'resolved':
            store.record_phase_result(
                lease, phase_intent, 'succeeded'
            )
        store.request_cancel(
            authorized.tool_call_id,
            proposal.authority,
            current_lease=lease,
        )
    finally:
        store.close()
    connection = _writer_connection(database)
    try:
        replacement = {
            'fake': 'fake-operation',
            'missing': None,
            'resolved': phase_intent.operation_id,
        }[target]
        connection.execute(
            'UPDATE room_mission_executions '
            'SET cancel_target_operation_id = ?',
            (replacement,),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RoomMissionLedgerSchemaError):
        SQLiteRoomMissionStore(str(database), clock=_Clock())


def test_cancel_late_result_corruption_fails_reopen(tmp_path) -> None:
    """The exact late adapter outcome is bound through cancellation."""
    database = tmp_path / 'cancel-late-result-corrupt.sqlite3'
    store = SQLiteRoomMissionStore(str(database), clock=_Clock())
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        lease = store.claim_execution(
            authorized.tool_call_id, proposal.authority, 'worker-a'
        )
        phase_intent = store.prepare_phase(lease, 'preflight')
        requested = store.request_cancel(
            authorized.tool_call_id,
            proposal.authority,
            current_lease=lease,
        )
        store.record_phase_result(lease, phase_intent, 'succeeded')
        store.record_cancel_result(
            lease, requested.intent, 'succeeded'
        )
    finally:
        store.close()
    connection = _writer_connection(database)
    try:
        connection.execute(
            '''
            UPDATE room_mission_events
            SET code = 'preflight_failed_late_discarded'
            WHERE event_kind = 'late_discarded'
            '''
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RoomMissionLedgerSchemaError):
        SQLiteRoomMissionStore(str(database), clock=_Clock())


@pytest.mark.parametrize(
    'target', ('commit_id', 'commit_revision', 'orphan_code')
)
def test_feedback_receipt_corruption_fails_reopen(
    tmp_path,
    target,
) -> None:
    """Terminal handoff receipts are bound to their exact outcomes."""
    database = tmp_path / f'feedback-receipt-{target}.sqlite3'
    store = SQLiteRoomMissionStore(str(database), clock=_Clock())
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        _finish_success(store, proposal.authority, authorized.tool_call_id)
        feedback_id = _feedback_id(authorized.tool_call_id)
        lease = store.claim_feedback(
            feedback_id, proposal.authority, 'feedback-worker'
        )
        if target == 'orphan_code':
            store.mark_feedback_orphaned(
                lease, 'conversation_missing'
            )
        else:
            store.mark_feedback_committed(
                lease, 'response-commit-1', 3
            )
    finally:
        store.close()
    connection = _writer_connection(database)
    try:
        guard_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'room_mission_feedback_terminal_update_guard'"
        ).fetchone()[0]
        connection.execute(
            'DROP TRIGGER room_mission_feedback_terminal_update_guard'
        )
        if target == 'commit_id':
            connection.execute(
                'UPDATE room_mission_feedback '
                "SET response_commit_id = 'forged-commit'"
            )
        elif target == 'commit_revision':
            connection.execute(
                'UPDATE room_mission_feedback '
                'SET conversation_revision_after = 4'
            )
        else:
            connection.execute(
                'UPDATE room_mission_feedback '
                "SET orphan_code = 'conversation_closed'"
            )
        connection.execute(guard_sql)
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RoomMissionLedgerSchemaError):
        SQLiteRoomMissionStore(str(database), clock=_Clock())


@pytest.mark.parametrize('terminal_state', ('committed', 'orphaned'))
def test_feedback_terminal_receipt_cannot_be_rewound(
    tmp_path,
    terminal_state,
) -> None:
    """Writer gates make every terminal handoff state append-only."""
    database = tmp_path / f'feedback-rewind-{terminal_state}.sqlite3'
    store = SQLiteRoomMissionStore(str(database), clock=_Clock())
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        _finish_success(store, proposal.authority, authorized.tool_call_id)
        feedback_id = _feedback_id(authorized.tool_call_id)
        lease = store.claim_feedback(
            feedback_id, proposal.authority, 'feedback-worker'
        )
        if terminal_state == 'committed':
            store.mark_feedback_committed(
                lease, 'response-commit-1', 2
            )
        else:
            store.mark_feedback_orphaned(
                lease, 'conversation_missing'
            )
    finally:
        store.close()
    connection = _writer_connection(database)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                '''
                UPDATE room_mission_feedback
                SET state = 'leased',
                    lease_owner = 'forged-worker',
                    lease_expires_at = 9999,
                    response_commit_id = NULL,
                    conversation_revision_after = NULL,
                    orphan_code = NULL,
                    result_digest = NULL
                '''
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute('DELETE FROM room_mission_feedback')
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                'INSERT OR REPLACE INTO room_mission_feedback '
                'SELECT * FROM room_mission_feedback'
            )
        connection.rollback()
    finally:
        connection.close()
    reopened = SQLiteRoomMissionStore(str(database), clock=_Clock())
    try:
        assert reopened.get_feedback(
            feedback_id, proposal.authority
        ).state == terminal_state
    finally:
        reopened.close()


def test_terminal_source_corruption_fails_reopen(tmp_path) -> None:
    """Terminal provenance is bound to the terminal payload digest."""
    database = tmp_path / 'terminal-source-corrupt.sqlite3'
    store = SQLiteRoomMissionStore(str(database), clock=_Clock())
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        _finish_success(store, proposal.authority, authorized.tool_call_id)
    finally:
        store.close()
    connection = _writer_connection(database)
    try:
        connection.execute(
            "UPDATE room_mission_events SET source = 'controller' "
            "WHERE event_kind = 'terminal'"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RoomMissionLedgerSchemaError):
        SQLiteRoomMissionStore(str(database), clock=_Clock())


def test_denial_is_owner_bound_exact_and_restart_safe(tmp_path) -> None:
    """Denial tombstones replay and never create execution records."""
    database = tmp_path / 'denial.sqlite3'
    clock = _Clock()
    proposal = _proposal()
    store = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        stored = store.register_proposal(proposal)
        denied = store.deny_proposal(
            stored.proposal_id, proposal.authority
        )
        assert denied.status == 'denied'
        assert denied.cached is False
        assert store.deny_proposal(
            stored.proposal_id, proposal.authority
        ) == replace(denied, cached=True)
        other = replace(
            proposal.authority,
            conversation_session_instance_id='other-instance',
        )
        with pytest.raises(RoomMissionLedgerAuthorityError) as caught:
            store.deny_proposal(stored.proposal_id, other)
        assert stored.proposal_id not in str(caught.value)
        with pytest.raises(RoomMissionLedgerConflictError):
            store.consume_confirmation(
                stored.proposal_id,
                proposal.authority,
                _confirmation(proposal),
            )
    finally:
        store.close()
    reopened = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        assert reopened.deny_proposal(
            stored.proposal_id, proposal.authority
        ) == replace(denied, cached=True)
    finally:
        reopened.close()
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            'SELECT COUNT(*) FROM room_mission_confirmations'
        ).fetchone()[0] == 0
        assert connection.execute(
            'SELECT COUNT(*) FROM room_mission_executions'
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_denial_expiry_and_confirmed_state_fail_closed() -> None:
    """Expired denial becomes timeout and confirmed work is immutable."""
    clock = _Clock()
    store = SQLiteRoomMissionStore(':memory:', clock=clock)
    try:
        expired_proposal = _proposal(expires_at=1001.0)
        expired = store.register_proposal(expired_proposal)
        clock.value = 1001.0
        timeout = store.deny_proposal(
            expired.proposal_id, expired_proposal.authority
        )
        assert timeout.status == 'timed_out'
        assert store.deny_proposal(
            expired.proposal_id, expired_proposal.authority
        ) == replace(timeout, cached=True)
    finally:
        store.close()

    store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    try:
        proposal, stored, _confirmation_value, _authorized_value = (
            _authorized(store)
        )
        with pytest.raises(RoomMissionLedgerStateError):
            store.deny_proposal(stored.proposal_id, proposal.authority)
    finally:
        store.close()


def test_cancel_request_never_steals_live_lease_and_late_phase_loses() -> None:
    """Cancellation persists beside a phase and dominates its late result."""
    store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        lease = store.claim_execution(
            authorized.tool_call_id, proposal.authority, 'worker-a'
        )
        phase_intent = store.prepare_phase(lease, 'preflight')
        requested = store.request_cancel(
            authorized.tool_call_id,
            proposal.authority,
            worker_id='cancel-controller',
            current_lease=replace(lease, lease_token='x' * 32),
        )
        assert isinstance(requested, CancellationRequest)
        assert isinstance(requested.intent, CancelIntent)
        assert requested.lease is None
        assert requested.pending_lease is True
        current = store.get_execution(
            authorized.tool_call_id, proposal.authority
        )
        assert current.status == 'cancelling'
        assert current.cancel_requested is True
        assert current.active_operation_id == phase_intent.operation_id

        exact = store.request_cancel(
            authorized.tool_call_id,
            proposal.authority,
            current_lease=lease,
        )
        assert exact.intent.operation_id == requested.intent.operation_id
        assert exact.intent.cached is True
        assert exact.lease == lease
        late = store.record_phase_result(
            lease, phase_intent, 'succeeded'
        )
        assert late.status == 'cancelling'
        assert late.active_operation_id is None
        assert late.cancel_requested is True
        assert store.record_phase_result(
            lease, phase_intent, 'succeeded'
        ) == late
        events = store.list_events(
            authorized.tool_call_id, proposal.authority
        )
        assert events[-1].event_kind == 'late_discarded'

        cancel_intent = store.get_cancel_intent(lease)
        terminal = store.record_cancel_result(
            lease, cancel_intent, 'succeeded'
        )
        assert terminal.status == 'cancelled'
        assert terminal.code == 'simulation_cancelled'
        assert len(store.list_feedback(
            proposal.authority, states=('pending',)
        )) == 1
        assert store.record_cancel_result(
            lease, cancel_intent, 'succeeded'
        ) == terminal
        with pytest.raises(RoomMissionLedgerConflictError):
            store.record_phase_result(
                lease, phase_intent, 'failed'
            )
        with pytest.raises(RoomMissionLedgerConflictError):
            store.record_cancel_result(lease, cancel_intent, 'failed')
        with pytest.raises(RoomMissionLedgerBusyError):
            store.record_cancel_result(
                replace(lease, lease_token='x' * 32),
                cancel_intent,
                'succeeded',
            )
    finally:
        store.close()


def test_cancel_terminal_supersedes_unobserved_phase_across_restart(
    tmp_path,
) -> None:
    """Cancel-first completion durably resolves an in-flight phase."""
    database = tmp_path / 'cancel-supersedes-phase.sqlite3'
    clock = _Clock()
    store = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        lease = store.claim_execution(
            authorized.tool_call_id, proposal.authority, 'worker-a'
        )
        phase_intent = store.prepare_phase(lease, 'preflight')
        requested = store.request_cancel(
            authorized.tool_call_id,
            proposal.authority,
            current_lease=lease,
        )
        assert requested.intent.superseded_phase_operation_id == (
            phase_intent.operation_id
        )
        fabricated = replace(
            requested.intent,
            superseded_phase_operation_id=_operation_id(
                authorized.tool_call_id, 'navigating'
            ),
        )
        with pytest.raises(RoomMissionLedgerConflictError):
            store.record_cancel_result(
                lease, fabricated, 'succeeded'
            )
        with pytest.raises(RoomMissionLedgerConflictError):
            store.record_cancel_result(
                lease,
                replace(
                    requested.intent,
                    state_revision=(
                        requested.intent.state_revision + 1
                    ),
                ),
                'succeeded',
            )
        terminal = store.record_cancel_result(
            lease, requested.intent, 'succeeded'
        )
        assert terminal.status == 'cancelled'
        assert store.record_phase_result(
            lease, phase_intent, 'succeeded'
        ) == terminal
    finally:
        store.close()

    reopened = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        assert reopened.get_execution(
            authorized.tool_call_id, proposal.authority
        ) == terminal
        assert reopened.record_phase_result(
            lease, phase_intent, 'succeeded'
        ) == terminal
        events = reopened.list_events(
            authorized.tool_call_id, proposal.authority
        )
        assert events[-1].event_kind == 'terminal'
        assert events[-1].source == 'simulation_adapter'
    finally:
        reopened.close()


def test_phase_and_cancel_results_serialize_to_cancelled(tmp_path) -> None:
    """Concurrent adapter callbacks cannot overturn cancellation."""
    database = tmp_path / 'cancel-result-race.sqlite3'
    clock = _Clock()
    first = SQLiteRoomMissionStore(str(database), clock=clock)
    second = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            first
        )
        lease = first.claim_execution(
            authorized.tool_call_id, proposal.authority, 'worker-a'
        )
        phase_intent = first.prepare_phase(lease, 'preflight')
        cancel_intent = first.request_cancel(
            authorized.tool_call_id,
            proposal.authority,
            current_lease=lease,
        ).intent
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def phase_result():
            try:
                barrier.wait()
                results.append(first.record_phase_result(
                    lease, phase_intent, 'succeeded'
                ))
            except Exception as error:
                errors.append(error)

        def cancel_result():
            try:
                barrier.wait()
                results.append(second.record_cancel_result(
                    lease, cancel_intent, 'succeeded'
                ))
            except Exception as error:
                errors.append(error)

        threads = [
            threading.Thread(target=phase_result),
            threading.Thread(target=cancel_result),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert len(results) == 2
        assert first.get_execution(
            authorized.tool_call_id, proposal.authority
        ).status == 'cancelled'
    finally:
        first.close()
        second.close()

    reopened = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        assert reopened.get_execution(
            authorized.tool_call_id, proposal.authority
        ).status == 'cancelled'
    finally:
        reopened.close()


def test_prepare_phase_rejects_cancel_between_phases() -> None:
    """A cancellation request blocks every fresh adapter dispatch."""
    store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        lease = store.claim_execution(
            authorized.tool_call_id, proposal.authority, 'worker-a'
        )
        preflight = store.prepare_phase(lease, 'preflight')
        store.record_phase_result(lease, preflight, 'succeeded')
        requested = store.request_cancel(
            authorized.tool_call_id,
            proposal.authority,
            current_lease=lease,
        )
        assert requested.intent.superseded_phase_operation_id is None
        with pytest.raises(RoomMissionLedgerStateError):
            store.prepare_phase(lease, 'navigating')
    finally:
        store.close()


@pytest.mark.parametrize(
    ('outcome', 'status', 'code'),
    (
        ('succeeded', 'cancelled', 'simulation_cancelled'),
        ('failed', 'failed', 'simulation_cancel_failed'),
        ('timed_out', 'timed_out', 'simulation_cancel_timeout'),
    ),
)
def test_cancel_without_lease_requires_fenced_claim(
    outcome,
    status,
    code,
) -> None:
    """Authority commits intent, while only execution claim mints bearer."""
    clock = _Clock()
    store = SQLiteRoomMissionStore(':memory:', clock=clock)
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        requested = store.request_cancel(
            authorized.tool_call_id,
            proposal.authority,
            worker_id='label-only',
        )
        assert requested.lease is None
        assert requested.pending_lease is True
        if outcome == 'timed_out':
            clock.value = 1007.1
        claimed = store.claim_execution(
            authorized.tool_call_id,
            proposal.authority,
            'trusted-cancel-worker',
        )
        assert claimed.recovery_required is True
        intent = store.get_cancel_intent(claimed)
        assert intent.operation_id == requested.intent.operation_id
        terminal = store.record_cancel_result(claimed, intent, outcome)
        assert terminal.status == status
        assert terminal.code == code
    finally:
        store.close()


def test_cancel_intent_survives_restart_and_is_recoverable(tmp_path) -> None:
    """A crash after intent commit never remints cancellation identity."""
    database = tmp_path / 'cancel-restart.sqlite3'
    clock = _Clock()
    store = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        requested = store.request_cancel(
            authorized.tool_call_id, proposal.authority
        )
    finally:
        store.close()
    reopened = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        candidates = reopened.list_recovery_candidates(
            proposal.authority
        )
        assert len(candidates) == 1
        assert candidates[0].cancel_requested is True
        assert candidates[0].cancel_operation_id == (
            requested.intent.operation_id
        )
        claimed = reopened.claim_execution(
            authorized.tool_call_id,
            proposal.authority,
            'cancel-recovery',
        )
        recovered = reopened.get_cancel_intent(claimed)
        assert recovered == replace(requested.intent, cached=True)
        replay = reopened.request_cancel(
            authorized.tool_call_id,
            proposal.authority,
            current_lease=claimed,
        )
        assert replay.intent == replace(requested.intent, cached=True)
        assert reopened.record_cancel_result(
            claimed, recovered, 'succeeded'
        ).status == 'cancelled'
    finally:
        reopened.close()


def test_concurrent_cancel_request_has_one_stable_event(tmp_path) -> None:
    """Two owner retries serialize to one cancellation transition."""
    database = tmp_path / 'cancel-concurrent.sqlite3'
    clock = _Clock()
    first = SQLiteRoomMissionStore(str(database), clock=clock)
    second = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            first
        )
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def cancel(store):
            try:
                barrier.wait()
                results.append(store.request_cancel(
                    authorized.tool_call_id, proposal.authority
                ))
            except Exception as error:
                errors.append(error)

        threads = [
            threading.Thread(target=cancel, args=(store,))
            for store in (first, second)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert not errors, repr(errors)
        assert len({item.intent.operation_id for item in results}) == 1
        assert sorted(item.intent.cached for item in results) == [
            False,
            True,
        ]
        events = first.list_events(
            authorized.tool_call_id, proposal.authority
        )
        assert sum(event.event_kind == 'cancel' for event in events) == 1
    finally:
        first.close()
        second.close()


def test_concurrent_denial_and_confirmation_have_one_winner(tmp_path) -> None:
    """Denial and confirmation serialize without a split-brain outcome."""
    database = tmp_path / 'deny-confirm.sqlite3'
    clock = _Clock()
    first = SQLiteRoomMissionStore(str(database), clock=clock)
    second = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        proposal = _proposal()
        stored = first.register_proposal(proposal)
        confirmation = _confirmation(proposal)
        barrier = threading.Barrier(2)
        successes = []
        errors = []

        def deny():
            try:
                barrier.wait()
                successes.append(first.deny_proposal(
                    stored.proposal_id, proposal.authority
                ))
            except Exception as error:
                errors.append(error)

        def confirm():
            try:
                barrier.wait()
                successes.append(second.consume_confirmation(
                    stored.proposal_id,
                    proposal.authority,
                    confirmation,
                ))
            except Exception as error:
                errors.append(error)

        threads = [
            threading.Thread(target=deny),
            threading.Thread(target=confirm),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert len(successes) == 1
        assert len(errors) == 1
        assert isinstance(
            errors[0],
            (RoomMissionLedgerConflictError, RoomMissionLedgerStateError),
        )
        connection = sqlite3.connect(database)
        try:
            proposal_status = connection.execute(
                'SELECT status FROM room_mission_proposals'
            ).fetchone()[0]
            execution_count = connection.execute(
                'SELECT COUNT(*) FROM room_mission_executions'
            ).fetchone()[0]
        finally:
            connection.close()
        assert (proposal_status, execution_count) in {
            ('denied', 0),
            ('confirmed', 1),
        }
    finally:
        first.close()
        second.close()


def test_cancel_is_owner_hidden_and_cap_terminal_is_atomic(
    monkeypatch,
) -> None:
    """Cross-owner cancellation hides IDs and the cap releases safely."""
    store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        other = replace(
            proposal.authority,
            conversation_session_instance_id='other-instance',
        )
        with pytest.raises(RoomMissionLedgerAuthorityError) as caught:
            store.request_cancel(authorized.tool_call_id, other)
        assert authorized.tool_call_id not in str(caught.value)
        monkeypatch.setattr(ledger_module, 'MAX_EVENTS_PER_MISSION', 2)
        with pytest.raises(RoomMissionLedgerStateError):
            store.request_cancel(
                authorized.tool_call_id, proposal.authority
            )
        terminal = store.get_execution(
            authorized.tool_call_id, proposal.authority
        )
        assert terminal.status == 'failed'
        assert terminal.code == 'event_capacity_reached'
        feedback = store.list_feedback(
            proposal.authority, states=('pending',)
        )
        assert len(feedback) == 1
    finally:
        store.close()


def test_feedback_claim_reopen_takeover_and_stale_fencing(tmp_path) -> None:
    """Feedback leases replay with receipt and fence expired workers."""
    database = tmp_path / 'feedback-lease.sqlite3'
    clock = _Clock()
    first = SQLiteRoomMissionStore(
        str(database), clock=clock, lease_seconds=0.5
    )
    second = SQLiteRoomMissionStore(
        str(database), clock=clock, lease_seconds=0.5
    )
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            first
        )
        _finish_success(
            first, proposal.authority, authorized.tool_call_id
        )
        feedback_id = _feedback_id(authorized.tool_call_id)
        pending = first.list_feedback(
            proposal.authority, states=('pending',)
        )
        assert len(pending) == 1
        assert pending[0].feedback_id == feedback_id
        claimed = first.claim_feedback(
            feedback_id, proposal.authority, 'feedback-worker-a'
        )
        assert isinstance(claimed, FeedbackLease)
        exact = second.claim_feedback(
            feedback_id,
            proposal.authority,
            'feedback-worker-a',
            prior_lease=claimed,
        )
        assert exact == replace(claimed, cached=True)
        with pytest.raises(RoomMissionLedgerBusyError):
            second.claim_feedback(
                feedback_id,
                proposal.authority,
                'feedback-worker-b',
            )
        clock.value += 0.51
        with pytest.raises(RoomMissionLedgerBusyError):
            second.claim_feedback(
                feedback_id,
                proposal.authority,
                'feedback-worker-a',
                prior_lease=claimed,
            )
        takeover = second.claim_feedback(
            feedback_id,
            proposal.authority,
            'feedback-worker-b',
        )
        assert takeover.lease_epoch == claimed.lease_epoch + 1
        with pytest.raises(RoomMissionLedgerBusyError):
            first.mark_feedback_committed(
                claimed, 'response-commit-a', 2
            )
        committed = second.mark_feedback_committed(
            takeover, 'response-commit-b', 2
        )
        assert committed.state == 'committed'
    finally:
        first.close()
        second.close()

    reopened = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        stored = reopened.get_feedback(
            feedback_id, proposal.authority
        )
        assert stored.state == 'committed'
        assert stored.response_commit_id == 'response-commit-b'
    finally:
        reopened.close()


def test_feedback_commit_and_orphan_have_exact_terminal_retries() -> None:
    """Terminal handoff outcomes are immutable behind receipt fencing."""
    store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        _finish_success(store, proposal.authority, authorized.tool_call_id)
        feedback_id = _feedback_id(authorized.tool_call_id)
        lease = store.claim_feedback(
            feedback_id, proposal.authority, 'feedback-worker'
        )
        committed = store.mark_feedback_committed(
            lease, 'response-commit-1', 3
        )
        replay = store.mark_feedback_committed(
            lease, 'response-commit-1', 3
        )
        assert replay == replace(committed, cached=True)
        with pytest.raises(RoomMissionLedgerConflictError):
            store.mark_feedback_committed(
                lease, 'response-commit-2', 3
            )
        with pytest.raises(RoomMissionLedgerConflictError):
            store.mark_feedback_orphaned(lease, 'conversation_missing')
    finally:
        store.close()

    store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        _finish_success(store, proposal.authority, authorized.tool_call_id)
        feedback_id = _feedback_id(authorized.tool_call_id)
        lease = store.claim_feedback(
            feedback_id, proposal.authority, 'feedback-worker'
        )
        orphaned = store.mark_feedback_orphaned(
            lease, 'conversation_reset'
        )
        assert orphaned.state == 'orphaned'
        assert orphaned.orphan_code == 'conversation_reset'
        assert store.mark_feedback_orphaned(
            lease, 'conversation_reset'
        ) == replace(orphaned, cached=True)
        with pytest.raises(RoomMissionLedgerConflictError):
            store.mark_feedback_orphaned(lease, 'conversation_closed')
    finally:
        store.close()


def test_feedback_commit_requires_forward_conversation_revision() -> None:
    """A committed response cannot move the owner revision backward."""
    store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        _finish_success(store, proposal.authority, authorized.tool_call_id)
        lease = store.claim_feedback(
            _feedback_id(authorized.tool_call_id),
            proposal.authority,
            'feedback-worker',
        )
        with pytest.raises(RoomMissionLedgerStateError):
            store.mark_feedback_committed(
                lease,
                'response-commit-stale',
                proposal.authority.conversation_revision,
            )
        assert store.mark_feedback_committed(
            lease,
            'response-commit-current',
            proposal.authority.conversation_revision + 1,
        ).state == 'committed'
    finally:
        store.close()


def test_feedback_fence_precedes_global_commit_id_checks() -> None:
    """Forged bearers cannot probe rows or another owner's commit IDs."""
    store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    try:
        first, _stored, _confirmation_value, first_authorized = (
            _authorized(store)
        )
        _finish_success(
            store, first.authority, first_authorized.tool_call_id
        )
        first_lease = store.claim_feedback(
            _feedback_id(first_authorized.tool_call_id),
            first.authority,
            'feedback-worker-1',
        )
        store.mark_feedback_committed(
            first_lease, 'response-commit-private', 2
        )

        second_authority = _authority(
            subject_id='owner-2',
            auth_session_id='auth-session-2',
            conversation_id='conversation-2',
            conversation_instance='instance-2',
            turn_id='turn-2',
            request_id='request-2',
        )
        second = _proposal(
            second_authority,
            decision_id='decision-2',
            device_id='simulation-device-2',
        )
        _proposal_value, _stored, _confirmation_value, second_authorized = (
            _authorized(
                store,
                second,
                confirmation_id='confirmation-2',
            )
        )
        _finish_success(
            store,
            second.authority,
            second_authorized.tool_call_id,
        )
        second_lease = store.claim_feedback(
            _feedback_id(second_authorized.tool_call_id),
            second.authority,
            'feedback-worker-2',
        )
        forged = replace(second_lease, lease_token='x' * 32)
        missing = replace(
            forged,
            feedback_id='room-feedback-' + ('f' * 64),
        )
        messages = []
        for candidate, response_id in (
            (forged, 'response-commit-private'),
            (forged, 'response-commit-unused'),
            (missing, 'response-commit-private'),
        ):
            with pytest.raises(RoomMissionLedgerBusyError) as caught:
                store.mark_feedback_committed(
                    candidate, response_id, 2
                )
            messages.append(str(caught.value))
        assert len(set(messages)) == 1
        with pytest.raises(RoomMissionLedgerConflictError):
            store.mark_feedback_committed(
                second_lease, 'response-commit-private', 2
            )
    finally:
        store.close()


def test_feedback_claim_is_owner_hidden_without_id_disclosure() -> None:
    """Another authority cannot list, read, or claim feedback IDs."""
    store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        _finish_success(store, proposal.authority, authorized.tool_call_id)
        feedback_id = _feedback_id(authorized.tool_call_id)
        other = replace(
            proposal.authority,
            conversation_session_instance_id='other-instance',
        )
        assert store.list_feedback(other) == ()
        with pytest.raises(RoomMissionLedgerAuthorityError) as caught:
            store.get_feedback(feedback_id, other)
        assert feedback_id not in str(caught.value)
        with pytest.raises(RoomMissionLedgerAuthorityError) as caught:
            store.claim_feedback(
                feedback_id, other, 'feedback-worker'
            )
        assert feedback_id not in str(caught.value)
    finally:
        store.close()


def test_concurrent_feedback_commit_is_one_exact_receipt(tmp_path) -> None:
    """Two commit retries serialize without duplicating handoff state."""
    database = tmp_path / 'feedback-concurrent.sqlite3'
    clock = _Clock()
    first = SQLiteRoomMissionStore(str(database), clock=clock)
    second = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            first
        )
        _finish_success(
            first, proposal.authority, authorized.tool_call_id
        )
        lease = first.claim_feedback(
            _feedback_id(authorized.tool_call_id),
            proposal.authority,
            'feedback-worker',
        )
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def commit(store):
            try:
                barrier.wait()
                results.append(store.mark_feedback_committed(
                    lease, 'response-commit-1', 4
                ))
            except Exception as error:
                errors.append(error)

        threads = [
            threading.Thread(target=commit, args=(store,))
            for store in (first, second)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert not errors, repr(errors)
        assert sorted(item.cached for item in results) == [False, True]
        assert {item.response_commit_id for item in results} == {
            'response-commit-1'
        }
    finally:
        first.close()
        second.close()


def test_concurrent_feedback_claim_has_one_lease_winner(tmp_path) -> None:
    """Two dispatchers cannot both claim one pending handoff."""
    database = tmp_path / 'feedback-claim-concurrent.sqlite3'
    clock = _Clock()
    first = SQLiteRoomMissionStore(str(database), clock=clock)
    second = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            first
        )
        _finish_success(
            first, proposal.authority, authorized.tool_call_id
        )
        feedback_id = _feedback_id(authorized.tool_call_id)
        barrier = threading.Barrier(2)
        leases = []
        errors = []

        def claim(store, worker):
            try:
                barrier.wait()
                leases.append(store.claim_feedback(
                    feedback_id, proposal.authority, worker
                ))
            except Exception as error:
                errors.append(error)

        threads = [
            threading.Thread(
                target=claim,
                args=(store, f'feedback-worker-{index}'),
            )
            for index, store in enumerate((first, second))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert len(leases) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], RoomMissionLedgerBusyError)
        assert first.get_feedback(
            feedback_id, proposal.authority
        ).lease_epoch == 1
    finally:
        first.close()
        second.close()


def test_feedback_prior_lease_replays_after_restart(tmp_path) -> None:
    """A retained opaque receipt survives process restart unchanged."""
    database = tmp_path / 'feedback-restart.sqlite3'
    clock = _Clock()
    store = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        _finish_success(store, proposal.authority, authorized.tool_call_id)
        feedback_id = _feedback_id(authorized.tool_call_id)
        lease = store.claim_feedback(
            feedback_id, proposal.authority, 'feedback-worker'
        )
    finally:
        store.close()
    reopened = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        replay = reopened.claim_feedback(
            feedback_id,
            proposal.authority,
            'feedback-worker',
            prior_lease=lease,
        )
        assert replay == replace(lease, cached=True)
        assert reopened.mark_feedback_committed(
            replay, 'response-commit-1', 5
        ).state == 'committed'
    finally:
        reopened.close()


def test_writer_wait_fences_cancel_result_after_lease_expiry(
    tmp_path,
) -> None:
    """A blocked cancellation observation cannot use a stale lease."""
    database = tmp_path / 'cancel-lock.sqlite3'
    now = time.time()
    proposal = _proposal(issued_at=now, expires_at=now + 2.0)
    confirmation = _confirmation(
        proposal,
        issued_at=now + 0.01,
        expires_at=now + 2.0,
    )
    store = SQLiteRoomMissionStore(
        str(database), lease_seconds=0.05
    )
    blocker = sqlite3.connect(database)
    try:
        stored = store.register_proposal(proposal)
        authorized = store.consume_confirmation(
            stored.proposal_id, proposal.authority, confirmation
        )
        lease = store.claim_execution(
            authorized.tool_call_id, proposal.authority, 'worker-a'
        )
        requested = store.request_cancel(
            authorized.tool_call_id,
            proposal.authority,
            current_lease=lease,
        )
        blocker.execute('BEGIN IMMEDIATE')
        errors = []

        def record():
            try:
                store.record_cancel_result(
                    lease, requested.intent, 'succeeded'
                )
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=record)
        thread.start()
        time.sleep(0.15)
        blocker.rollback()
        thread.join(timeout=5)
        assert len(errors) == 1
        assert isinstance(errors[0], RoomMissionLedgerBusyError)
        assert store.get_execution(
            authorized.tool_call_id, proposal.authority
        ).status == 'cancelling'
    finally:
        blocker.close()
        store.close()


def test_writer_wait_fences_feedback_commit_after_lease_expiry(
    tmp_path,
) -> None:
    """Feedback commit checks lease time after acquiring writer lock."""
    database = tmp_path / 'feedback-lock.sqlite3'
    now = time.time()
    proposal = _proposal(issued_at=now, expires_at=now + 2.0)
    confirmation = _confirmation(
        proposal,
        issued_at=now + 0.01,
        expires_at=now + 2.0,
    )
    store = SQLiteRoomMissionStore(
        str(database), lease_seconds=0.05
    )
    blocker = sqlite3.connect(database)
    try:
        stored = store.register_proposal(proposal)
        authorized = store.consume_confirmation(
            stored.proposal_id, proposal.authority, confirmation
        )
        _finish_success(store, proposal.authority, authorized.tool_call_id)
        feedback_id = _feedback_id(authorized.tool_call_id)
        lease = store.claim_feedback(
            feedback_id, proposal.authority, 'feedback-worker'
        )
        blocker.execute('BEGIN IMMEDIATE')
        errors = []

        def commit():
            try:
                store.mark_feedback_committed(
                    lease, 'response-commit-1', 2
                )
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=commit)
        thread.start()
        time.sleep(0.15)
        blocker.rollback()
        thread.join(timeout=5)
        assert len(errors) == 1
        assert isinstance(errors[0], RoomMissionLedgerBusyError)
        assert store.get_feedback(
            feedback_id, proposal.authority
        ).state == 'leased'
    finally:
        blocker.close()
        store.close()


def test_feedback_bearer_is_digest_only_at_rest(tmp_path) -> None:
    """Opaque feedback delivery capabilities never enter SQLite raw."""
    database = tmp_path / 'feedback-privacy.sqlite3'
    store = SQLiteRoomMissionStore(str(database), clock=_Clock())
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        _finish_success(store, proposal.authority, authorized.tool_call_id)
        lease = store.claim_feedback(
            _feedback_id(authorized.tool_call_id),
            proposal.authority,
            'feedback-worker',
        )
    finally:
        store.close()
    durable_bytes = b''.join(
        (tmp_path / name).read_bytes()
        for name in os.listdir(tmp_path)
        if name.startswith('feedback-privacy.sqlite3')
    )
    assert lease.lease_token.encode('utf-8') not in durable_bytes


def test_predispatch_abort_releases_device_and_outboxes(tmp_path) -> None:
    """A clean controller abort terminalizes without adapter provenance."""
    assert ABORT_EXECUTION_CODES == frozenset({
        'authority_revoked',
        'state_unavailable',
        'state_stale',
        'privacy_blocked',
        'emergency_stop',
        'map_changed',
        'device_unavailable',
    })
    assert RECONCILIATION_FAILURE_CODE == 'recovery_unavailable'
    database = tmp_path / 'abort-clean.sqlite3'
    clock = _Clock()
    store = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        with pytest.raises(RoomMissionLedgerValidationError):
            store.abort_execution(
                authorized.tool_call_id,
                proposal.authority,
                'private-custom-reason',
            )
        terminal = store.abort_execution(
            authorized.tool_call_id,
            proposal.authority,
            'authority_revoked',
        )
        assert terminal.status == 'failed'
        assert terminal.code == 'authority_revoked'
        assert store.abort_execution(
            authorized.tool_call_id,
            proposal.authority,
            'authority_revoked',
        ) == terminal
        with pytest.raises(RoomMissionLedgerConflictError):
            store.abort_execution(
                authorized.tool_call_id,
                proposal.authority,
                'privacy_blocked',
            )
        events = store.list_events(
            authorized.tool_call_id, proposal.authority
        )
        assert events[-1].event_kind == 'terminal'
        assert events[-1].source == 'controller'
        assert all(
            event.source != 'simulation_adapter' for event in events
        )
        assert len(store.list_feedback(
            proposal.authority, states=('pending',)
        )) == 1

        next_authority = _authority(
            turn_id='turn-2', request_id='request-2', revision=2
        )
        next_proposal = _proposal(
            next_authority, decision_id='decision-2'
        )
        _authorized(
            store,
            next_proposal,
            confirmation_id='confirmation-2',
        )
    finally:
        store.close()

    reopened = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        assert reopened.abort_execution(
            authorized.tool_call_id,
            proposal.authority,
            'authority_revoked',
        ) == terminal
    finally:
        reopened.close()


def test_abort_is_owner_hidden_and_validates_optional_lease() -> None:
    """Abort ownership is hidden and cannot steal a live clean lease."""
    store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        lease = store.claim_execution(
            authorized.tool_call_id, proposal.authority, 'worker-a'
        )
        other = replace(
            proposal.authority,
            conversation_session_instance_id='other-instance',
        )
        with pytest.raises(RoomMissionLedgerAuthorityError) as caught:
            store.abort_execution(
                authorized.tool_call_id, other, 'emergency_stop'
            )
        assert authorized.tool_call_id not in str(caught.value)
        with pytest.raises(RoomMissionLedgerAuthorityError) as missing:
            store.abort_execution(
                'room-tool-call-' + ('f' * 64),
                other,
                'emergency_stop',
            )
        assert str(missing.value) == str(caught.value)
        with pytest.raises(RoomMissionLedgerBusyError):
            store.abort_execution(
                authorized.tool_call_id,
                proposal.authority,
                'emergency_stop',
            )
        with pytest.raises(RoomMissionLedgerBusyError):
            store.abort_execution(
                authorized.tool_call_id,
                proposal.authority,
                'emergency_stop',
                current_lease=replace(lease, lease_token='x' * 32),
            )
        assert store.get_execution(
            authorized.tool_call_id, proposal.authority
        ).lease_expires_at == lease.expires_at
        terminal = store.abort_execution(
            authorized.tool_call_id,
            proposal.authority,
            'emergency_stop',
            current_lease=lease,
        )
        assert terminal.status == 'failed'
        assert terminal.code == 'emergency_stop'
    finally:
        store.close()


def test_abort_active_intent_marks_recovery_without_lease_theft() -> None:
    """An unresolved operation is fenced for recovery, never claimed done."""
    store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        lease = store.claim_execution(
            authorized.tool_call_id, proposal.authority, 'worker-a'
        )
        phase_intent = store.prepare_phase(lease, 'preflight')
        with pytest.raises(RoomMissionLedgerBusyError):
            store.abort_execution(
                authorized.tool_call_id,
                proposal.authority,
                'state_unavailable',
            )
        marked = store.abort_execution(
            authorized.tool_call_id,
            proposal.authority,
            'state_unavailable',
            current_lease=lease,
        )
        assert marked.status == 'reconcile_required'
        assert marked.active_operation_id == phase_intent.operation_id
        assert marked.lease_epoch == lease.lease_epoch
        assert marked.lease_expires_at == lease.expires_at
        event_count = len(store.list_events(
            authorized.tool_call_id, proposal.authority
        ))
        assert store.abort_execution(
            authorized.tool_call_id,
            proposal.authority,
            'state_unavailable',
            current_lease=lease,
        ) == marked
        assert len(store.list_events(
            authorized.tool_call_id, proposal.authority
        )) == event_count
        with pytest.raises(RoomMissionLedgerConflictError):
            store.abort_execution(
                authorized.tool_call_id,
                proposal.authority,
                'state_stale',
            )
        recovery = store.get_recovery_intent(lease)
        assert recovery.operation_id == phase_intent.operation_id
        events = store.list_events(
            authorized.tool_call_id, proposal.authority
        )
        assert events[-1].event_kind == 'recovery'
        assert events[-1].source == 'recovery'
        assert not any(
            event.event_kind == 'terminal' for event in events
        )
    finally:
        store.close()


def test_fail_phase_reconciliation_is_restart_exact(tmp_path) -> None:
    """A recovery observation failure terminalizes without adapter claims."""
    database = tmp_path / 'phase-recovery-failed.sqlite3'
    clock = _Clock()
    store = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        lease = store.claim_execution(
            authorized.tool_call_id, proposal.authority, 'worker-a'
        )
        store.prepare_phase(lease, 'preflight')
        store.abort_execution(
            authorized.tool_call_id,
            proposal.authority,
            'state_unavailable',
            current_lease=lease,
        )
        recovery_lease = lease
        recovery_intent = store.get_recovery_intent(recovery_lease)
        with pytest.raises(RoomMissionLedgerValidationError):
            store.fail_reconciliation(
                recovery_lease,
                recovery_intent,
                code='private-recovery-detail',
            )
        terminal = store.fail_reconciliation(
            recovery_lease, recovery_intent
        )
        assert terminal.status == 'failed'
        assert terminal.code == 'recovery_unavailable'
        assert store.fail_reconciliation(
            recovery_lease, recovery_intent
        ) == terminal
        events = store.list_events(
            authorized.tool_call_id, proposal.authority
        )
        assert events[-1].event_kind == 'terminal'
        assert events[-1].source == 'recovery'
        assert events[-1].operation_id == recovery_intent.operation_id
        assert not any(
            event.source == 'simulation_adapter' for event in events
        )
        assert len(store.list_feedback(
            proposal.authority, states=('pending',)
        )) == 1
    finally:
        store.close()

    reopened = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        assert reopened.fail_reconciliation(
            recovery_lease, recovery_intent
        ) == terminal
    finally:
        reopened.close()


def test_fail_reconciliation_rejects_forged_typestate_and_stale_lease(
    tmp_path,
) -> None:
    """Caller flags, wrong operations, and replaced epochs never authorize."""
    database = tmp_path / 'recovery-fencing.sqlite3'
    clock = _Clock()
    store = SQLiteRoomMissionStore(
        str(database), clock=clock, lease_seconds=0.5
    )
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        lease = store.claim_execution(
            authorized.tool_call_id, proposal.authority, 'worker-a'
        )
        phase_intent = store.prepare_phase(lease, 'preflight')
        forged_flag = replace(lease, recovery_required=True)
        fabricated_recovery = RecoveryPhaseIntent(
            tool_call_id=phase_intent.tool_call_id,
            phase=phase_intent.phase,
            operation_id=phase_intent.operation_id,
            state_revision=phase_intent.state_revision,
        )
        with pytest.raises(RoomMissionLedgerStateError):
            store.fail_reconciliation(
                forged_flag, fabricated_recovery
            )
        store.abort_execution(
            authorized.tool_call_id,
            proposal.authority,
            'state_stale',
            current_lease=lease,
        )
        recovery_lease = lease
        recovery_intent = store.get_recovery_intent(recovery_lease)
        wrong = replace(
            recovery_intent,
            phase='navigating',
            operation_id=_operation_id(
                authorized.tool_call_id, 'navigating'
            ),
        )
        with pytest.raises(RoomMissionLedgerStateError):
            store.fail_reconciliation(recovery_lease, wrong)
        with pytest.raises(RoomMissionLedgerBusyError):
            store.fail_reconciliation(
                replace(recovery_lease, lease_token='x' * 32),
                wrong,
            )
        clock.value += 0.51
        replacement = store.claim_execution(
            authorized.tool_call_id,
            proposal.authority,
            'recovery-b',
        )
        with pytest.raises(RoomMissionLedgerBusyError):
            store.fail_reconciliation(
                recovery_lease, recovery_intent
            )
        current_intent = store.get_recovery_intent(replacement)
        assert store.fail_reconciliation(
            replacement, current_intent
        ).status == 'failed'
    finally:
        store.close()


@pytest.mark.parametrize('with_active_phase', (False, True))
def test_fail_cancel_reconciliation_uses_cancel_operation(
    tmp_path,
    with_active_phase,
) -> None:
    """Cancellation recovery is fenced by its separate stable operation."""
    database = tmp_path / f'cancel-recovery-{with_active_phase}.sqlite3'
    clock = _Clock()
    store = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        lease = None
        if with_active_phase:
            lease = store.claim_execution(
                authorized.tool_call_id,
                proposal.authority,
                'worker-a',
            )
            store.prepare_phase(lease, 'preflight')
        requested = store.request_cancel(
            authorized.tool_call_id,
            proposal.authority,
            current_lease=lease,
        )
        cancel_lease = (
            lease
            if lease is not None
            else store.claim_execution(
                authorized.tool_call_id,
                proposal.authority,
                'cancel-recovery',
            )
        )
        event_count = len(store.list_events(
            authorized.tool_call_id, proposal.authority
        ))
        still_cancelling = store.abort_execution(
            authorized.tool_call_id,
            proposal.authority,
            'emergency_stop',
        )
        assert still_cancelling.status == 'cancelling'
        assert len(store.list_events(
            authorized.tool_call_id, proposal.authority
        )) == event_count
        cancel_intent = store.get_cancel_intent(cancel_lease)
        assert cancel_intent == replace(requested.intent, cached=True)
        terminal = store.fail_reconciliation(
            cancel_lease, cancel_intent
        )
        assert terminal.status == 'failed'
        assert terminal.code == 'recovery_unavailable'
        events = store.list_events(
            authorized.tool_call_id, proposal.authority
        )
        assert events[-1].source == 'recovery'
        assert events[-1].operation_id == cancel_intent.operation_id
        assert store.fail_reconciliation(
            cancel_lease, cancel_intent
        ) == terminal
    finally:
        store.close()
    reopened = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        assert reopened.get_execution(
            authorized.tool_call_id, proposal.authority
        ) == terminal
    finally:
        reopened.close()


def test_abort_and_recovery_failure_reserve_terminal_slot(
    monkeypatch,
) -> None:
    """The abort marker leaves one bounded event for recovery failure."""
    clock = _Clock()
    store = SQLiteRoomMissionStore(':memory:', clock=clock)
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            store
        )
        lease = store.claim_execution(
            authorized.tool_call_id, proposal.authority, 'worker-a'
        )
        store.prepare_phase(lease, 'preflight')
        monkeypatch.setattr(ledger_module, 'MAX_EVENTS_PER_MISSION', 5)
        store.abort_execution(
            authorized.tool_call_id,
            proposal.authority,
            'device_unavailable',
            current_lease=lease,
        )
        recovery_lease = lease
        recovery_intent = store.get_recovery_intent(recovery_lease)
        terminal = store.fail_reconciliation(
            recovery_lease, recovery_intent
        )
        assert terminal.status == 'failed'
        events = store.list_events(
            authorized.tool_call_id, proposal.authority
        )
        assert len(events) == 5
        assert events[-1].code == 'recovery_unavailable'
        assert events[-1].source == 'recovery'
    finally:
        store.close()


def test_concurrent_abort_and_cancel_result_have_one_terminal(
    tmp_path,
) -> None:
    """A controller abort cannot duplicate a cancellation terminal commit."""
    database = tmp_path / 'abort-cancel-race.sqlite3'
    clock = _Clock()
    first = SQLiteRoomMissionStore(str(database), clock=clock)
    second = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        proposal, _stored, _confirmation_value, authorized = _authorized(
            first
        )
        requested = first.request_cancel(
            authorized.tool_call_id, proposal.authority
        )
        lease = first.claim_execution(
            authorized.tool_call_id,
            proposal.authority,
            'cancel-worker',
        )
        cancel_intent = first.get_cancel_intent(lease)
        barrier = threading.Barrier(2)
        errors = []

        def abort():
            try:
                barrier.wait()
                first.abort_execution(
                    authorized.tool_call_id,
                    proposal.authority,
                    'emergency_stop',
                )
            except Exception as error:
                errors.append(error)

        def cancel_result():
            try:
                barrier.wait()
                second.record_cancel_result(
                    lease, cancel_intent, 'succeeded'
                )
            except Exception as error:
                errors.append(error)

        threads = [
            threading.Thread(target=abort),
            threading.Thread(target=cancel_result),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert all(not thread.is_alive() for thread in threads)
        assert all(
            isinstance(error, RoomMissionLedgerConflictError)
            for error in errors
        )
        terminal = first.get_execution(
            authorized.tool_call_id, proposal.authority
        )
        assert terminal.status == 'cancelled'
        assert terminal.code == 'simulation_cancelled'
        events = first.list_events(
            authorized.tool_call_id, proposal.authority
        )
        assert sum(
            event.event_kind == 'terminal' for event in events
        ) == 1
        assert len(first.list_feedback(
            proposal.authority, states=('pending',)
        )) == 1
        assert requested.intent.operation_id == cancel_intent.operation_id
    finally:
        first.close()
        second.close()


def test_proposal_invalidation_replays_without_execution(tmp_path) -> None:
    """System invalidation is durable and never creates a Tool ID."""
    assert PROPOSAL_INVALIDATION_CODES == frozenset({
        'authority_revoked',
        'source_changed',
        'map_changed',
        'device_changed',
    })
    database = tmp_path / 'proposal-invalidated.sqlite3'
    clock = _Clock()
    proposal = _proposal()
    store = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        created = store.register_proposal(proposal)
        failed = store.invalidate_proposal(
            created.proposal_id,
            proposal.authority,
            'source_changed',
        )
        assert failed.status == 'failed'
        assert failed.cached is False
        assert store.invalidate_proposal(
            created.proposal_id,
            proposal.authority,
            'source_changed',
        ) == replace(failed, cached=True)
        registered = store.register_proposal(proposal)
        assert registered == replace(failed, cached=True)
        assert not hasattr(registered, 'tool_call_id')
    finally:
        store.close()

    reopened = SQLiteRoomMissionStore(str(database), clock=clock)
    try:
        assert reopened.invalidate_proposal(
            created.proposal_id,
            proposal.authority,
            'source_changed',
        ) == replace(failed, cached=True)
    finally:
        reopened.close()

    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            '''
            SELECT status, terminal_code
            FROM room_mission_proposals
            WHERE proposal_id = ?
            ''',
            (created.proposal_id,),
        ).fetchone()
        assert row == ('failed', 'source_changed')
        assert row[1] != 'user_denied'
        for table in (
            'room_mission_confirmations',
            'room_mission_executions',
            'room_mission_feedback',
        ):
            assert connection.execute(
                f'SELECT COUNT(*) FROM {table}'
            ).fetchone()[0] == 0
    finally:
        connection.close()


def test_proposal_invalidation_is_private_and_terminal_states_hold() -> None:
    """Owner checks and existing proposal outcomes fail closed."""
    clock = _Clock()
    store = SQLiteRoomMissionStore(':memory:', clock=clock)
    proposal = _proposal()
    try:
        stored = store.register_proposal(proposal)
        other = replace(
            proposal.authority,
            conversation_session_instance_id='other-instance',
        )
        with pytest.raises(RoomMissionLedgerAuthorityError) as hidden:
            store.invalidate_proposal(
                stored.proposal_id, other, 'map_changed'
            )
        with pytest.raises(RoomMissionLedgerAuthorityError) as missing:
            store.invalidate_proposal(
                'room-proposal-' + ('f' * 64),
                other,
                'map_changed',
            )
        assert str(hidden.value) == str(missing.value)
        assert stored.proposal_id not in str(hidden.value)
        with pytest.raises(RoomMissionLedgerValidationError):
            store.invalidate_proposal(
                stored.proposal_id,
                proposal.authority,
                'private-source-detail',
            )
        store.invalidate_proposal(
            stored.proposal_id,
            proposal.authority,
            'map_changed',
        )
        with pytest.raises(RoomMissionLedgerConflictError):
            store.invalidate_proposal(
                stored.proposal_id,
                proposal.authority,
                'device_changed',
            )
    finally:
        store.close()

    with pytest.raises(RoomMissionLedgerError) as caught:
        store.invalidate_proposal(
            stored.proposal_id,
            proposal.authority,
            'map_changed',
        )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None

    denied_store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    try:
        denied_proposal = _proposal()
        denied = denied_store.register_proposal(denied_proposal)
        denied_store.deny_proposal(
            denied.proposal_id, denied_proposal.authority
        )
        with pytest.raises(RoomMissionLedgerStateError):
            denied_store.invalidate_proposal(
                denied.proposal_id,
                denied_proposal.authority,
                'authority_revoked',
            )
    finally:
        denied_store.close()

    timeout_clock = _Clock()
    timeout_store = SQLiteRoomMissionStore(
        ':memory:', clock=timeout_clock
    )
    try:
        timed_proposal = _proposal(expires_at=1001.0)
        timed = timeout_store.register_proposal(timed_proposal)
        timeout_clock.value = 1001.0
        timeout_store.deny_proposal(
            timed.proposal_id, timed_proposal.authority
        )
        with pytest.raises(RoomMissionLedgerStateError):
            timeout_store.invalidate_proposal(
                timed.proposal_id,
                timed_proposal.authority,
                'source_changed',
            )
    finally:
        timeout_store.close()

    confirmed_store = SQLiteRoomMissionStore(':memory:', clock=_Clock())
    try:
        confirmed_proposal, stored, _confirmation_value, authorized = (
            _authorized(confirmed_store)
        )
        with pytest.raises(RoomMissionLedgerStateError):
            confirmed_store.invalidate_proposal(
                stored.proposal_id,
                confirmed_proposal.authority,
                'device_changed',
            )
        assert confirmed_store.get_execution(
            authorized.tool_call_id, confirmed_proposal.authority
        ).status == 'pending'
    finally:
        confirmed_store.close()


def test_confirmation_and_invalidation_have_one_32_way_winner(
    tmp_path,
) -> None:
    """Thirty-two writers reveal at most one durable authorization result."""
    database = tmp_path / 'invalidate-confirm-race.sqlite3'
    clock = _Clock()
    proposal = _proposal()
    confirmation = _confirmation(proposal)
    owner = SQLiteRoomMissionStore(str(database), clock=clock)
    stored = owner.register_proposal(proposal)
    stores = [owner] + [
        SQLiteRoomMissionStore(str(database), clock=clock)
        for _index in range(31)
    ]
    barrier = threading.Barrier(32)
    confirmations = []
    invalidations = []
    errors = []
    result_lock = threading.Lock()

    def compete(index):
        try:
            barrier.wait()
            if index % 2 == 0:
                result = stores[index].consume_confirmation(
                    stored.proposal_id,
                    proposal.authority,
                    confirmation,
                )
                with result_lock:
                    confirmations.append(result)
            else:
                result = stores[index].invalidate_proposal(
                    stored.proposal_id,
                    proposal.authority,
                    'authority_revoked',
                )
                with result_lock:
                    invalidations.append(result)
        except Exception as error:
            with result_lock:
                errors.append(error)

    threads = [
        threading.Thread(target=compete, args=(index,))
        for index in range(32)
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert all(not thread.is_alive() for thread in threads)
        row = owner._connection.execute(
            'SELECT status, terminal_code '
            'FROM room_mission_proposals WHERE proposal_id = ?',
            (stored.proposal_id,),
        ).fetchone()
        if str(row['status']) == 'failed':
            assert str(row['terminal_code']) == 'authority_revoked'
            assert len(invalidations) == 16
            assert confirmations == []
            assert all(
                isinstance(error, RoomMissionLedgerConflictError)
                for error in errors
            )
            assert owner._connection.execute(
                'SELECT COUNT(*) FROM room_mission_executions'
            ).fetchone()[0] == 0
            assert owner._connection.execute(
                'SELECT COUNT(*) FROM room_mission_confirmations'
            ).fetchone()[0] == 0
            assert owner._connection.execute(
                'SELECT COUNT(*) FROM room_mission_feedback'
            ).fetchone()[0] == 0
            assert all(
                stored.proposal_id not in str(error)
                for error in errors
            )
        else:
            assert str(row['status']) == 'confirmed'
            assert row['terminal_code'] is None
            assert len(confirmations) == 16
            assert invalidations == []
            assert len({
                result.tool_call_id for result in confirmations
            }) == 1
            assert all(
                isinstance(error, RoomMissionLedgerStateError)
                for error in errors
            )
            assert owner._connection.execute(
                'SELECT COUNT(*) FROM room_mission_executions'
            ).fetchone()[0] == 1
            assert owner._connection.execute(
                "SELECT status FROM room_mission_executions"
            ).fetchone()[0] == 'pending'
        assert len(errors) == 16
    finally:
        for store in stores:
            store.close()


def test_invalid_proposal_terminal_code_corruption_fails_reopen(
    tmp_path,
) -> None:
    """Failed proposal provenance cannot be changed into user denial."""
    database = tmp_path / 'proposal-terminal-code.sqlite3'
    store = SQLiteRoomMissionStore(str(database), clock=_Clock())
    try:
        proposal = _proposal()
        stored = store.register_proposal(proposal)
        store.invalidate_proposal(
            stored.proposal_id,
            proposal.authority,
            'device_changed',
        )
    finally:
        store.close()
    connection = _writer_connection(database)
    try:
        connection.execute(
            "UPDATE room_mission_proposals "
            "SET terminal_code = 'user_denied'"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RoomMissionLedgerSchemaError):
        SQLiteRoomMissionStore(str(database), clock=_Clock())
