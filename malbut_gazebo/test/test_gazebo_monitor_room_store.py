"""Tests for the Gazebo-only durable monitor-room state core."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import os
import sqlite3
from threading import Barrier

import pytest

from malbut_gazebo.gazebo_monitor_room_store import (
    CancelOperation,
    DISPATCH_CLAIM_NO_UPDATE_TRIGGER_SQL,
    DispatchClaimEvidence,
    EVENT_NO_UPDATE_TRIGGER_SQL,
    GAZEBO_MONITOR_ROOM_MAX_SAMPLES,
    GAZEBO_MONITOR_ROOM_SCHEMA_VERSION,
    GazeboMonitorRoomClockRollbackError,
    GazeboMonitorRoomBootIdentityError,
    GazeboMonitorRoomConflictError,
    GazeboMonitorRoomDeadlineError,
    GazeboMonitorRoomDurabilityError,
    GazeboMonitorRoomFenceError,
    GazeboMonitorRoomLeaseError,
    GazeboMonitorRoomSchemaError,
    GazeboMonitorRoomStore,
    GazeboMonitorRoomStoreError,
    GazeboMonitorRoomValidationError,
    GoalTransition,
    METADATA_IMMUTABLE_TRIGGER_SQL,
    OPERATION_IDENTITY_TRIGGER_SQL,
    OPERATION_TRANSITION_TRIGGER_SQL,
    OrderedSemanticSample,
    PrepareOperation,
    PrivateOperationBinding,
    _event_digest,
    _dispatch_claim_digest,
    _operation_digest,
    _sample_digest,
    stable_goal_uuid,
)


_DIGEST = 'a' * 64
_OTHER_DIGEST = 'b' * 64
_WIRE_DIGEST = 'c' * 64
_BOOT_ONE = '11111111-2222-3333-4444-555555555555'
_BOOT_TWO = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


def _boot_one():
    return _BOOT_ONE


def _request(
    *,
    prepare_request_id='prepare-1',
    operation_id='operation-1',
    robot_id='robot-1',
    deadline=100.0,
    samples=None,
    plan_digest=_DIGEST,
):
    return PrepareOperation(
        prepare_request_id=prepare_request_id,
        operation_id=operation_id,
        robot_id=robot_id,
        map_id='home-map',
        map_revision='map-revision-1',
        semantic_revision='semantic-revision-1',
        zones_digest=_DIGEST,
        target_binding_digest=_DIGEST,
        effects_digest=_DIGEST,
        profile_digest=_DIGEST,
        plan_digest=plan_digest,
        ordered_semantic_samples=(
            samples
            if samples is not None
            else (
                OrderedSemanticSample(0, 0, 0, 1000, 2000),
                OrderedSemanticSample(1, 0, 1, 3000, 4000),
            )
        ),
        deadline=deadline,
    )


def _lease(store, *, now=2.0, worker='worker-1', expected_fence=0):
    return store.acquire_lease(
        'operation-1',
        worker_id=worker,
        expected_fence=expected_fence,
        lease_seconds=20.0,
        now=now,
    )


def _to_navigating(store, *, start=3.0, worker='worker-1'):
    token = store.transition_token('operation-1', worker_id=worker)
    store.begin_preflight(token, now=start)
    token = store.transition_token('operation-1', worker_id=worker)
    store.record_send_intent(
        token, preflight_digest=_DIGEST, now=start + 1
    )
    token = store.transition_token('operation-1', worker_id=worker)
    return store.record_navigating(
        token, acceptance_digest=_DIGEST, now=start + 2
    )


def _to_send_intent(store, *, start=3.0, worker='worker-1'):
    token = store.transition_token('operation-1', worker_id=worker)
    store.begin_preflight(token, now=start)
    token = store.transition_token('operation-1', worker_id=worker)
    return store.record_send_intent(
        token, preflight_digest=_DIGEST, now=start + 1
    )


def _claim_start(store, *, now=5.0, **overrides):
    values = {
        'start_fingerprint': _DIGEST,
        'binding_digest': store.private_operation_binding(
            'operation-1'
        ).binding_digest,
        'preflight_digest': _DIGEST,
        'wire_payload_digest': _WIRE_DIGEST,
        'now': now,
    }
    values.update(overrides)
    return store.claim_start_dispatch(
        store.transition_token('operation-1', worker_id='worker-1'),
        **values,
    )


def _cancel_request(store, request_id='cancel-1', reason='operator_requested'):
    return CancelOperation(
        cancel_request_id=request_id,
        transition=store.transition_token(
            'operation-1', worker_id='worker-1'
        ),
        reason_code=reason,
    )


def test_strict_frozen_dtos_and_public_non_claims(tmp_path):
    """Inputs stay bounded and observations cannot imply physical coverage."""
    with pytest.raises(GazeboMonitorRoomValidationError):
        OrderedSemanticSample(0, 0, 0, True, 0)
    with pytest.raises(GazeboMonitorRoomValidationError):
        OrderedSemanticSample(0, 0, 0, 0, 0, frame_id='odom')
    with pytest.raises(GazeboMonitorRoomValidationError):
        replace(_request(), ordered_semantic_samples=[])
    with pytest.raises(FrozenInstanceError):
        _request().operation_id = 'changed'

    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    result = store.prepare(_request(), now=1.0)
    public = result.to_public_dict()
    assert public['runtime_mode'] == 'gazebo'
    assert public['simulation'] is True
    for key in (
        'physical_authorized',
        'physical_effects',
        'viewer_live',
        'camera_coverage_validated',
        'coverage_achieved',
        'all_navigation_samples_reached',
    ):
        assert public[key] is False
    serialized = repr(public)
    assert 'x_mm' not in serialized
    assert 'y_mm' not in serialized
    assert 'route' not in serialized
    private = store.private_current_sample('operation-1')
    assert (private.x_mm, private.y_mm) == (1000, 2000)


def test_private_operation_binding_returns_exact_redacted_evidence(tmp_path):
    """The adapter seam returns exact persisted fields without repr leaks."""
    request = _request()
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(request, now=1.0)

    binding = store.private_operation_binding(request.operation_id)
    expected = PrivateOperationBinding(
        operation_id=request.operation_id,
        prepare_fingerprint=request.payload_fingerprint,
        robot_id=request.robot_id,
        map_id=request.map_id,
        map_revision=request.map_revision,
        semantic_revision=request.semantic_revision,
        zones_digest=request.zones_digest,
        target_binding_digest=request.target_binding_digest,
        effects_digest=request.effects_digest,
        profile_digest=request.profile_digest,
        plan_digest=request.plan_digest,
        sample_count=len(request.ordered_semantic_samples),
        deadline=request.deadline,
    )
    assert binding == expected
    assert binding.schema_version == GAZEBO_MONITOR_ROOM_SCHEMA_VERSION
    assert binding.runtime_mode == 'gazebo'
    assert binding.simulation is True
    assert binding.binding_digest == expected.binding_digest
    assert repr(binding) == (
        'PrivateOperationBinding(schema_version=3, '
        "runtime_mode='gazebo', simulation=True, "
        'physical_authorized=False, physical_effects=False, '
        'viewer_live=False, camera_coverage_validated=False, '
        'coverage_achieved=False)'
    )
    assert not hasattr(binding, 'to_public_dict')
    assert not hasattr(binding, 'x_mm')
    assert not hasattr(binding, 'y_mm')


def test_private_operation_binding_rejects_mutation_and_weak_types(tmp_path):
    """Frozen bypasses and bool-as-int binding fields fail closed."""
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(_request(), now=1.0)
    binding = store.private_operation_binding('operation-1')

    with pytest.raises(FrozenInstanceError):
        binding.map_id = 'changed-map'
    with pytest.raises(GazeboMonitorRoomValidationError):
        replace(binding, sample_count=True)
    with pytest.raises(GazeboMonitorRoomValidationError):
        replace(binding, deadline=True)

    mutated = replace(binding)
    object.__setattr__(mutated, 'robot_id', True)
    with pytest.raises(GazeboMonitorRoomValidationError):
        _ = mutated.binding_digest
    fixed_field_mutation = replace(binding)
    object.__setattr__(fixed_field_mutation, 'runtime_mode', 'physical')
    with pytest.raises(GazeboMonitorRoomValidationError):
        _ = fixed_field_mutation.binding_digest


def test_private_operation_binding_distinguishes_distinct_prepares(tmp_path):
    """Changed durable bindings and sample counts change their evidence."""
    first_request = _request()
    second_request = replace(
        _request(
            prepare_request_id='prepare-2',
            operation_id='operation-2',
            robot_id='robot-2',
            samples=(OrderedSemanticSample(0, 1, 0, 5000, 6000),),
            plan_digest=_OTHER_DIGEST,
        ),
        map_id='other-map',
        map_revision='map-revision-2',
        semantic_revision='semantic-revision-2',
        zones_digest=_OTHER_DIGEST,
        target_binding_digest=_OTHER_DIGEST,
        effects_digest=_OTHER_DIGEST,
        profile_digest=_OTHER_DIGEST,
    )
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(first_request, now=1.0)
    store.prepare(second_request, now=1.0)

    first = store.private_operation_binding('operation-1')
    second = store.private_operation_binding('operation-2')
    assert first.map_id == 'home-map'
    assert second.map_id == 'other-map'
    assert (first.sample_count, second.sample_count) == (2, 1)
    assert first.prepare_fingerprint != second.prepare_fingerprint
    assert first.binding_digest != second.binding_digest
    assert replace(first, sample_count=1).binding_digest != (
        first.binding_digest
    )


def test_private_operation_binding_obeys_store_lifecycle_and_path(tmp_path):
    """Binding reads retain closed-store and durable-path fail-closed rules."""
    closed = GazeboMonitorRoomStore(tmp_path / 'closed.sqlite3')
    closed.prepare(_request(), now=1.0)
    closed.close()
    with pytest.raises(GazeboMonitorRoomStoreError):
        closed.private_operation_binding('operation-1')

    path = tmp_path / 'drift.sqlite3'
    drifted = GazeboMonitorRoomStore(path)
    drifted.prepare(_request(), now=1.0)
    path.chmod(0o640)
    with pytest.raises(GazeboMonitorRoomDurabilityError):
        drifted.private_operation_binding('operation-1')


def test_private_operation_binding_supports_concurrent_reads(tmp_path):
    """Multiple adapter workers receive one exact immutable binding."""
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(_request(), now=1.0)
    expected = store.private_operation_binding('operation-1')
    barrier = Barrier(8)

    def read_binding(_index):
        barrier.wait()
        return store.private_operation_binding('operation-1')

    with ThreadPoolExecutor(max_workers=8) as executor:
        bindings = list(executor.map(read_binding, range(8)))
    assert bindings == [expected] * 8


def test_mutated_frozen_prepare_cannot_reuse_cached_fingerprint(tmp_path):
    """Object-level frozen bypass cannot split identity from stored payload."""
    first_sample = OrderedSemanticSample(0, 0, 0, 1000, 2000)
    request = _request(
        samples=(
            first_sample,
            OrderedSemanticSample(1, 0, 1, 3000, 4000),
        )
    )
    original_fingerprint = request.payload_fingerprint
    object.__setattr__(request, 'map_revision', 'forged-revision')
    object.__setattr__(first_sample, 'x_mm', 999999)
    assert request.payload_fingerprint == original_fingerprint

    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    with pytest.raises(GazeboMonitorRoomValidationError):
        store.prepare(request, now=1.0)
    fresh = store.prepare(_request(), now=1.0)
    assert fresh.replayed is False
    assert store.private_current_sample('operation-1').x_mm == 1000


def test_mutated_cancel_and_goal_tokens_are_recanonicalized(tmp_path):
    """Nested cached cancel data and mutated CAS fields fail closed."""
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(_request(), now=1.0)
    _lease(store)

    token = store.transition_token('operation-1', worker_id='worker-1')
    object.__setattr__(token, 'sample_index', True)
    with pytest.raises(GazeboMonitorRoomValidationError):
        store.begin_preflight(token, now=3.0)

    cancel = _cancel_request(store)
    object.__setattr__(cancel, 'reason_code', 'forged_reason')
    with pytest.raises(GazeboMonitorRoomValidationError):
        store.request_cancel(cancel, now=3.0)

    nested = _cancel_request(store, request_id='cancel-2')
    object.__setattr__(nested.transition, 'worker_id', 'worker-forged')
    with pytest.raises(GazeboMonitorRoomValidationError):
        store.request_cancel(nested, now=3.0)


def test_sample_count_is_bounded_and_order_must_be_exact():
    """A semantic candidate tuple cannot be empty, reordered, or unbounded."""
    too_many = tuple(
        OrderedSemanticSample(index, 0, index, index, index)
        for index in range(GAZEBO_MONITOR_ROOM_MAX_SAMPLES)
    ) + (OrderedSemanticSample(0, 0, 0, 0, 0),)
    with pytest.raises(GazeboMonitorRoomValidationError):
        _request(samples=too_many)
    with pytest.raises(GazeboMonitorRoomValidationError):
        _request(
            samples=(
                OrderedSemanticSample(1, 0, 0, 0, 0),
                OrderedSemanticSample(0, 0, 1, 1, 1),
            )
        )


def test_prepare_exact_replay_conflict_and_one_blocking_robot(tmp_path):
    """Prepare is exact-once and one robot has one unresolved operation."""
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    request = _request()
    first = store.prepare(request, now=1.0)
    replay = store.prepare(request, now=2.0)
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.operation_id == first.operation_id
    expired_replay = store.prepare(request, now=request.deadline)
    assert expired_replay.replayed is True

    with pytest.raises(GazeboMonitorRoomConflictError):
        store.prepare(replace(request, plan_digest=_OTHER_DIGEST), now=3.0)
    with pytest.raises(GazeboMonitorRoomConflictError):
        store.prepare(
            _request(
                prepare_request_id='prepare-2',
                operation_id='operation-2',
            ),
            now=3.0,
        )


def test_stable_goal_uuid_survives_reopen_and_fence_takeover(tmp_path):
    """Lease fencing never changes a sample's deterministic goal UUID."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(path)
    prepared = store.prepare(_request(), now=1.0)
    expected = stable_goal_uuid(store.store_namespace, 'operation-1', 0)
    assert prepared.current_goal_uuid == expected
    first = _lease(store, now=2.0)
    store.close()

    reopened = GazeboMonitorRoomStore(path)
    assert reopened.observe('operation-1').current_goal_uuid == expected
    takeover = reopened.acquire_lease(
        'operation-1',
        worker_id='worker-2',
        expected_fence=first.fence_epoch,
        lease_seconds=10.0,
        now=first.lease_expires_at,
    )
    assert takeover.fence_epoch == first.fence_epoch + 1
    assert takeover.taken_over is True
    assert takeover.observation.current_goal_uuid == expected
    assert stable_goal_uuid(
        reopened.store_namespace, 'operation-1', 1
    ) != expected


def test_store_namespace_and_exact_boot_identity_survive_reopen(tmp_path):
    """A store has one random namespace bound to one canonical boot UUID."""
    path = tmp_path / 'state.sqlite3'
    reader = _boot_one
    store = GazeboMonitorRoomStore(path, boot_id_reader=reader)
    namespace = store.store_namespace
    prepared = store.prepare(_request(), now=1.0)
    assert len(namespace) == 32
    assert prepared.current_goal_uuid == stable_goal_uuid(
        namespace, 'operation-1', 0
    )
    store.close()

    reopened = GazeboMonitorRoomStore(path, boot_id_reader=reader)
    assert reopened.store_namespace == namespace
    assert reopened.observe('operation-1').current_goal_uuid == (
        prepared.current_goal_uuid
    )
    reopened.close()
    with pytest.raises(GazeboMonitorRoomBootIdentityError):
        GazeboMonitorRoomStore(
            path,
            boot_id_reader=lambda: _BOOT_TWO,
        )


def test_boot_reader_failure_is_typed_content_free_and_chain_free(tmp_path):
    """Host read and format failures never disclose provider content."""
    def failed_reader():
        raise RuntimeError('/private/boot-id-secret')

    for reader in (failed_reader, lambda: '/private/boot-id-secret'):
        with pytest.raises(GazeboMonitorRoomBootIdentityError) as raised:
            GazeboMonitorRoomStore(
                tmp_path / f'state-{id(reader)}.sqlite3',
                boot_id_reader=reader,
            )
        assert str(raised.value) == 'host boot identity is unavailable'
        assert '/private' not in str(raised.value)
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None
        assert raised.value.__traceback__ is None


def test_database_recreation_gets_new_namespace_and_goal_uuid(tmp_path):
    """Recreated storage cannot collide with a retained Nav2 goal UUID."""
    path = tmp_path / 'state.sqlite3'
    reader = _boot_one
    first = GazeboMonitorRoomStore(path, boot_id_reader=reader)
    first_goal = first.prepare(_request(), now=1.0).current_goal_uuid
    first_namespace = first.store_namespace
    first.close()
    path.unlink()

    second = GazeboMonitorRoomStore(path, boot_id_reader=reader)
    second_goal = second.prepare(_request(), now=1.0).current_goal_uuid
    assert second.store_namespace != first_namespace
    assert second_goal != first_goal


@pytest.mark.parametrize('legacy_version', (1, 2))
def test_legacy_schema_is_rejected_without_silent_reinterpretation(
    tmp_path, legacy_version
):
    """The explicit v3 store never treats a legacy shape as fresh data."""
    path = tmp_path / 'legacy.sqlite3'
    connection = sqlite3.connect(path)
    connection.execute(
        '''
        CREATE TABLE gazebo_monitor_room_schema_metadata (
            singleton INTEGER PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            contract_digest TEXT NOT NULL
        )
        '''
    )
    connection.execute(
        '''
        INSERT INTO gazebo_monitor_room_schema_metadata
        VALUES (1, ?, ?)
        ''',
        (legacy_version, _DIGEST),
    )
    connection.commit()
    connection.close()
    path.chmod(0o600)

    with pytest.raises(GazeboMonitorRoomSchemaError):
        GazeboMonitorRoomStore(path, boot_id_reader=lambda: _BOOT_ONE)


def test_rehashed_metadata_boot_change_fails_as_boot_mismatch(tmp_path):
    """Restored exact schema cannot hide a valid-shape boot substitution."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(
        path, boot_id_reader=lambda: _BOOT_ONE
    )
    store.close()
    connection = sqlite3.connect(path)
    connection.execute(
        'DROP TRIGGER gazebo_monitor_room_metadata_immutable'
    )
    connection.execute(
        '''
        UPDATE gazebo_monitor_room_schema_metadata
        SET host_boot_id = ? WHERE singleton = 1
        ''',
        (_BOOT_TWO,),
    )
    connection.execute(METADATA_IMMUTABLE_TRIGGER_SQL)
    connection.commit()
    connection.close()

    with pytest.raises(GazeboMonitorRoomBootIdentityError):
        GazeboMonitorRoomStore(
            path, boot_id_reader=lambda: _BOOT_ONE
        )


def test_lease_fence_clock_rollback_and_stale_callback(tmp_path):
    """Expired owners and clocks behind durable history fail closed."""
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(_request(), now=10.0)
    first = _lease(store, now=11.0)
    with pytest.raises(GazeboMonitorRoomLeaseError):
        store.acquire_lease(
            'operation-1',
            worker_id='worker-2',
            expected_fence=first.fence_epoch,
            lease_seconds=10.0,
            now=12.0,
        )
    takeover = store.acquire_lease(
        'operation-1',
        worker_id='worker-2',
        expected_fence=first.fence_epoch,
        lease_seconds=10.0,
        now=first.lease_expires_at,
    )
    stale = GoalTransition(
        operation_id='operation-1',
        worker_id='worker-1',
        fence_epoch=first.fence_epoch,
        sample_index=0,
        goal_uuid=first.observation.current_goal_uuid,
        expected_operation_state='prepared',
        expected_sample_state='pending',
    )
    with pytest.raises(GazeboMonitorRoomFenceError):
        store.begin_preflight(stale, now=first.lease_expires_at + 1)
    with pytest.raises(GazeboMonitorRoomClockRollbackError):
        store.acquire_lease(
            'operation-1',
            worker_id='worker-2',
            expected_fence=takeover.fence_epoch,
            lease_seconds=10.0,
            now=first.lease_expires_at - 1,
        )


def test_lease_renewal_never_shortens_the_current_fence(tmp_path):
    """A shorter renewal cannot create an early takeover window."""
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(_request(), now=1.0)
    first = _lease(store, now=2.0)
    renewed = store.acquire_lease(
        'operation-1',
        worker_id='worker-1',
        expected_fence=first.fence_epoch,
        lease_seconds=1.0,
        now=3.0,
    )
    assert renewed.fence_epoch == first.fence_epoch
    assert renewed.taken_over is False
    assert renewed.lease_expires_at == first.lease_expires_at


def test_goal_uuid_sample_and_state_are_all_cas_inputs(tmp_path):
    """A transition rejects a wrong UUID even with the right fence."""
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(_request(), now=1.0)
    _lease(store)
    token = store.transition_token('operation-1', worker_id='worker-1')
    wrong = replace(
        token,
        goal_uuid=stable_goal_uuid(
            store.store_namespace, 'different-op', 0
        ),
    )
    with pytest.raises(GazeboMonitorRoomConflictError):
        store.begin_preflight(wrong, now=3.0)
    wrong_state = replace(token, expected_sample_state='preflighting')
    with pytest.raises(GazeboMonitorRoomConflictError):
        store.begin_preflight(wrong_state, now=3.0)


def test_two_samples_checkpoint_without_coverage_claim(tmp_path):
    """Sequential Nav2 evidence advances exact counts, not room coverage."""
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(_request(), now=1.0)
    _lease(store)
    _to_navigating(store)
    first_goal = store.observe('operation-1').current_goal_uuid
    reached_one = store.record_sample_succeeded(
        store.transition_token('operation-1', worker_id='worker-1'),
        result_evidence_digest=_DIGEST,
        now=6.0,
    )
    assert reached_one.state == 'preflighting'
    assert reached_one.navigation_samples_reached == 1
    assert reached_one.current_sample_index == 1
    assert reached_one.current_goal_uuid != first_goal

    store.record_send_intent(
        store.transition_token('operation-1', worker_id='worker-1'),
        preflight_digest=_DIGEST,
        now=7.0,
    )
    store.record_navigating(
        store.transition_token('operation-1', worker_id='worker-1'),
        acceptance_digest=_DIGEST,
        now=8.0,
    )
    complete = store.record_sample_succeeded(
        store.transition_token('operation-1', worker_id='worker-1'),
        result_evidence_digest=_DIGEST,
        now=9.0,
    )
    assert complete.state == 'succeeded'
    assert complete.navigation_samples_reached == 2
    assert complete.all_navigation_samples_reached is True
    assert complete.coverage_achieved is False
    assert complete.camera_coverage_validated is False
    events = store.events('operation-1')
    assert [event.event_seq for event in events] == list(
        range(1, len(events) + 1)
    )


def test_send_intent_crash_reopens_without_automatic_resend(tmp_path):
    """A crash window remains a durable send intent for reconciliation."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(), now=1.0)
    _lease(store)
    token = store.transition_token('operation-1', worker_id='worker-1')
    store.begin_preflight(token, now=3.0)
    store.record_send_intent(
        store.transition_token('operation-1', worker_id='worker-1'),
        preflight_digest=_DIGEST,
        now=4.0,
    )
    goal_uuid = store.observe('operation-1').current_goal_uuid
    store.close()

    reopened = GazeboMonitorRoomStore(path)
    restored = reopened.observe('operation-1')
    assert restored.state == 'send_intent'
    assert restored.current_goal_uuid == goal_uuid
    assert not hasattr(reopened, 'resend')


def test_start_dispatch_claim_is_durable_one_shot_and_replay_closed(
    tmp_path,
):
    """Only the exact first start payload may cross the dispatch boundary."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(), now=1.0)
    _lease(store, now=2.0)
    _to_send_intent(store)

    assert _claim_start(store, now=5.0) is True
    assert _claim_start(store, now=6.0) is False
    assert _claim_start(
        store,
        now=6.0,
        wire_payload_digest=_OTHER_DIGEST,
    ) is False
    with pytest.raises(GazeboMonitorRoomClockRollbackError):
        store.acquire_lease(
            'operation-1',
            worker_id='worker-1',
            expected_fence=1,
            lease_seconds=20.0,
            now=4.0,
        )
    store.close()

    reopened = GazeboMonitorRoomStore(path)
    assert _claim_start(reopened, now=7.0) is False
    reopened_binding = reopened.private_operation_binding(
        'operation-1'
    ).binding_digest
    reopened_evidence = reopened.assert_start_dispatch_claim(
        reopened.transition_token(
            'operation-1', worker_id='worker-1'
        ),
        start_fingerprint=_DIGEST,
        binding_digest=reopened_binding,
        preflight_digest=_DIGEST,
        wire_payload_digest=_WIRE_DIGEST,
        now=7.0,
    )
    assert reopened_evidence.claimed_at == 5.0
    assert reopened_evidence.store_namespace == reopened.store_namespace
    assert _claim_start(
        reopened,
        now=101.0,
        start_fingerprint=_OTHER_DIGEST,
        wire_payload_digest=_OTHER_DIGEST,
    ) is False
    assert reopened.observe('operation-1').state == 'send_intent'


def test_two_connections_have_exactly_one_start_dispatch_winner(tmp_path):
    """BEGIN IMMEDIATE linearizes a cross-connection first claim."""
    path = tmp_path / 'state.sqlite3'
    first = GazeboMonitorRoomStore(path)
    first.prepare(_request(), now=1.0)
    _lease(first, now=2.0)
    _to_send_intent(first)
    second = GazeboMonitorRoomStore(path)
    barrier = Barrier(2)

    def claim(store):
        barrier.wait()
        return _claim_start(store, now=5.0)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(claim, (first, second)))
    assert sorted(results) == [False, True]


def test_start_dispatch_assertion_returns_exact_immutable_evidence(
    tmp_path,
):
    """The read-only proof binds claim, current rows, lease, and deadline."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(), now=1.0)
    lease = _lease(store, now=2.0)
    _to_send_intent(store)
    binding = store.private_operation_binding(
        'operation-1'
    ).binding_digest
    assert _claim_start(store, now=5.0) is True
    token = store.transition_token(
        'operation-1', worker_id='worker-1'
    )
    statements = []
    store._require_connection().set_trace_callback(statements.append)
    evidence = store.assert_start_dispatch_claim(
        token,
        start_fingerprint=_DIGEST,
        binding_digest=binding,
        preflight_digest=_DIGEST,
        wire_payload_digest=_WIRE_DIGEST,
        now=6.0,
    )
    store._require_connection().set_trace_callback(None)

    assert type(evidence) is DispatchClaimEvidence
    assert evidence.phase == 'start'
    assert evidence.store_namespace == store.store_namespace
    assert evidence.operation_id == 'operation-1'
    assert evidence.sample_index == 0
    assert evidence.goal_uuid == token.goal_uuid
    assert evidence.operation_state == 'send_intent'
    assert evidence.sample_state == 'send_intent'
    assert evidence.worker_id == 'worker-1'
    assert evidence.fence_epoch == lease.fence_epoch
    assert evidence.start_fingerprint == _DIGEST
    assert evidence.cancel_request_id is None
    assert evidence.cancel_request_fingerprint is None
    assert evidence.binding_digest == binding
    assert evidence.preflight_digest == _DIGEST
    assert evidence.wire_payload_digest == _WIRE_DIGEST
    assert evidence.claim_lease_expires_at == lease.lease_expires_at
    assert evidence.current_lease_expires_at == lease.lease_expires_at
    assert evidence.claimed_at == 5.0
    assert evidence.operation_deadline == 100.0
    assert evidence.checked_at == 6.0
    assert len(evidence.evidence_digest) == 64
    assert evidence.simulation is True
    for name in (
        'physical_authorized',
        'physical_effects',
        'viewer_live',
        'camera_coverage_validated',
        'coverage_achieved',
    ):
        assert getattr(evidence, name) is False

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    claim = connection.execute(
        'SELECT * FROM gazebo_monitor_room_dispatch_claims'
    ).fetchone()
    operation = connection.execute(
        'SELECT * FROM gazebo_monitor_room_operations'
    ).fetchone()
    sample = connection.execute(
        'SELECT * FROM gazebo_monitor_room_samples WHERE sample_index = 0'
    ).fetchone()
    connection.close()
    assert evidence.claim_record_digest == claim['record_digest']
    assert evidence.claim_record_digest == _dispatch_claim_digest(claim)
    assert evidence.operation_record_digest == operation['record_digest']
    assert evidence.operation_record_digest == _operation_digest(operation)
    assert evidence.sample_record_digest == sample['record_digest']
    assert evidence.sample_record_digest == _sample_digest(sample)

    traced = tuple(statement.strip().upper() for statement in statements)
    assert sum(statement == 'BEGIN' for statement in traced) == 1
    assert sum(statement == 'COMMIT' for statement in traced) == 1
    assert not any(
        statement.startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE'))
        for statement in traced
    )
    redacted = repr(evidence)
    for private in (
        evidence.operation_id,
        evidence.store_namespace,
        evidence.goal_uuid,
        evidence.binding_digest,
        '1000',
        '2000',
    ):
        assert private not in redacted
    with pytest.raises(GazeboMonitorRoomValidationError):
        replace(evidence, sample_index=True)
    with pytest.raises(GazeboMonitorRoomValidationError):
        replace(evidence, checked_at=True)
    with pytest.raises(GazeboMonitorRoomValidationError):
        replace(evidence, current_lease_expires_at=5.0)
    with pytest.raises(GazeboMonitorRoomValidationError):
        replace(evidence, claim_record_digest=_OTHER_DIGEST)
    with pytest.raises(FrozenInstanceError):
        evidence.phase = 'cancel'
    object.__setattr__(evidence, 'goal_uuid', '0' * 32)
    with pytest.raises(GazeboMonitorRoomValidationError):
        _ = evidence.evidence_digest


@pytest.mark.parametrize(
    ('field_name', 'value'),
    (
        ('start_fingerprint', _OTHER_DIGEST),
        ('binding_digest', _OTHER_DIGEST),
        ('preflight_digest', _OTHER_DIGEST),
        ('wire_payload_digest', _OTHER_DIGEST),
    ),
)
def test_start_dispatch_assertion_fails_closed_on_exact_mismatch(
    tmp_path, field_name, value
):
    """No caller fingerprint or digest may differ from the first claim."""
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(_request(), now=1.0)
    _lease(store, now=2.0)
    _to_send_intent(store)
    token = store.transition_token(
        'operation-1', worker_id='worker-1'
    )
    binding = store.private_operation_binding(
        'operation-1'
    ).binding_digest
    values = {
        'start_fingerprint': _DIGEST,
        'binding_digest': binding,
        'preflight_digest': _DIGEST,
        'wire_payload_digest': _WIRE_DIGEST,
        'now': 6.0,
    }
    with pytest.raises(GazeboMonitorRoomConflictError):
        store.assert_start_dispatch_claim(token, **values)
    assert _claim_start(store, now=5.0) is True
    values[field_name] = value
    with pytest.raises(GazeboMonitorRoomConflictError) as captured:
        store.assert_start_dispatch_claim(token, **values)
    assert value not in str(captured.value)
    assert captured.value.__cause__ is None


def test_start_dispatch_assertion_enforces_claim_lease_and_deadline(
    tmp_path,
):
    """A renewal cannot extend start authority beyond its claimed lease."""
    lease_path = tmp_path / 'claim-lease.sqlite3'
    store = GazeboMonitorRoomStore(lease_path)
    store.prepare(_request(), now=1.0)
    first_lease = store.acquire_lease(
        'operation-1',
        worker_id='worker-1',
        expected_fence=0,
        lease_seconds=4.0,
        now=2.0,
    )
    _to_send_intent(store)
    binding = store.private_operation_binding(
        'operation-1'
    ).binding_digest
    assert _claim_start(store, now=5.0) is True
    renewed = store.acquire_lease(
        'operation-1',
        worker_id='worker-1',
        expected_fence=first_lease.fence_epoch,
        lease_seconds=20.0,
        now=5.5,
    )
    token = store.transition_token(
        'operation-1', worker_id='worker-1'
    )
    evidence = store.assert_start_dispatch_claim(
        token,
        start_fingerprint=_DIGEST,
        binding_digest=binding,
        preflight_digest=_DIGEST,
        wire_payload_digest=_WIRE_DIGEST,
        now=5.75,
    )
    assert evidence.claim_lease_expires_at == first_lease.lease_expires_at
    assert evidence.current_lease_expires_at == renewed.lease_expires_at
    with pytest.raises(GazeboMonitorRoomLeaseError):
        store.assert_start_dispatch_claim(
            token,
            start_fingerprint=_DIGEST,
            binding_digest=binding,
            preflight_digest=_DIGEST,
            wire_payload_digest=_WIRE_DIGEST,
            now=first_lease.lease_expires_at,
        )

    deadline_store = GazeboMonitorRoomStore(
        tmp_path / 'deadline.sqlite3'
    )
    deadline_store.prepare(_request(deadline=10.0), now=1.0)
    _lease(deadline_store, now=2.0)
    _to_send_intent(deadline_store)
    deadline_binding = deadline_store.private_operation_binding(
        'operation-1'
    ).binding_digest
    assert _claim_start(deadline_store, now=5.0) is True
    with pytest.raises(GazeboMonitorRoomDeadlineError):
        deadline_store.assert_start_dispatch_claim(
            deadline_store.transition_token(
                'operation-1', worker_id='worker-1'
            ),
            start_fingerprint=_DIGEST,
            binding_digest=deadline_binding,
            preflight_digest=_DIGEST,
            wire_payload_digest=_WIRE_DIGEST,
            now=10.0,
        )


def test_dispatch_assertion_and_concurrent_renewal_are_snapshot_atomic(
    tmp_path,
):
    """Evidence is wholly before or after a competing durable renewal."""
    path = tmp_path / 'state.sqlite3'
    reader = GazeboMonitorRoomStore(path)
    reader.prepare(_request(), now=1.0)
    first_lease = _lease(reader, now=2.0)
    _to_send_intent(reader)
    binding = reader.private_operation_binding(
        'operation-1'
    ).binding_digest
    assert _claim_start(reader, now=5.0) is True
    writer = GazeboMonitorRoomStore(path)
    barrier = Barrier(2)

    def assert_claim():
        barrier.wait()
        return reader.assert_start_dispatch_claim(
            reader.transition_token(
                'operation-1', worker_id='worker-1'
            ),
            start_fingerprint=_DIGEST,
            binding_digest=binding,
            preflight_digest=_DIGEST,
            wire_payload_digest=_WIRE_DIGEST,
            now=6.0,
        )

    def renew():
        barrier.wait()
        return writer.acquire_lease(
            'operation-1',
            worker_id='worker-1',
            expected_fence=first_lease.fence_epoch,
            lease_seconds=30.0,
            now=6.0,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        proof_future = executor.submit(assert_claim)
        renewal_future = executor.submit(renew)
        evidence = proof_future.result()
        renewal = renewal_future.result()
    assert evidence.current_lease_expires_at in {
        first_lease.lease_expires_at,
        renewal.lease_expires_at,
    }
    assert evidence.checked_at < evidence.claim_lease_expires_at

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    matching_events = connection.execute(
        '''
        SELECT lease_expires_at FROM gazebo_monitor_room_events
        WHERE operation_id = ? AND operation_record_digest = ?
        ''',
        ('operation-1', evidence.operation_record_digest),
    ).fetchall()
    connection.close()
    assert any(
        row['lease_expires_at'] == evidence.current_lease_expires_at
        for row in matching_events
    )


def test_start_claim_takeover_or_changed_payload_never_resends(tmp_path):
    """A later fence cannot reinterpret an already claimed stable goal."""
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(_request(), now=1.0)
    lease = _lease(store, now=2.0)
    _to_send_intent(store)
    assert _claim_start(store, now=5.0) is True
    takeover = store.acquire_lease(
        'operation-1',
        worker_id='worker-2',
        expected_fence=lease.fence_epoch,
        lease_seconds=20.0,
        now=lease.lease_expires_at,
    )
    assert store.claim_start_dispatch(
        store.transition_token(
            'operation-1', worker_id='worker-2'
        ),
        start_fingerprint=_OTHER_DIGEST,
        binding_digest=store.private_operation_binding(
            'operation-1'
        ).binding_digest,
        preflight_digest=_DIGEST,
        wire_payload_digest=_WIRE_DIGEST,
        now=takeover.observation.updated_at,
    ) is False


def test_cancel_ready_and_claim_are_exact_but_ignore_operation_deadline(
    tmp_path,
):
    """Cancellation remains dispatchable after deadline under a live lease."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(deadline=10.0), now=1.0)
    _lease(store, now=2.0)
    _to_navigating(store)
    store.request_cancel(_cancel_request(store), now=6.0)
    token = store.transition_token('operation-1', worker_id='worker-1')

    ready = store.assert_cancel_ready(
        token, cancel_request_id='cancel-1', now=11.0
    )
    assert ready.state == 'cancel_requested'
    with pytest.raises(GazeboMonitorRoomConflictError):
        store.assert_cancel_ready(
            token, cancel_request_id='cancel-other', now=11.0
        )
    binding_digest = store.private_operation_binding(
        'operation-1'
    ).binding_digest
    assert store.claim_cancel_dispatch(
        token,
        cancel_request_id='cancel-1',
        request_fingerprint=_DIGEST,
        binding_digest=binding_digest,
        wire_payload_digest=_WIRE_DIGEST,
        now=12.0,
    ) is True
    assert store.claim_cancel_dispatch(
        token,
        cancel_request_id='cancel-1',
        request_fingerprint=_DIGEST,
        binding_digest=binding_digest,
        wire_payload_digest=_WIRE_DIGEST,
        now=13.0,
    ) is False
    assert store.claim_cancel_dispatch(
        token,
        cancel_request_id='cancel-1',
        request_fingerprint=_DIGEST,
        binding_digest=binding_digest,
        wire_payload_digest=_OTHER_DIGEST,
        now=13.0,
    ) is False
    store.close()
    reopened = GazeboMonitorRoomStore(path)
    assert reopened.claim_cancel_dispatch(
        reopened.transition_token(
            'operation-1', worker_id='worker-1'
        ),
        cancel_request_id='cancel-1',
        request_fingerprint=_DIGEST,
        binding_digest=reopened.private_operation_binding(
            'operation-1'
        ).binding_digest,
        wire_payload_digest=_WIRE_DIGEST,
        now=14.0,
    ) is False
    assert reopened.claim_cancel_dispatch(
        reopened.transition_token(
            'operation-1', worker_id='stale-worker'
        ),
        cancel_request_id='different-cancel-id',
        request_fingerprint=_OTHER_DIGEST,
        binding_digest=_OTHER_DIGEST,
        wire_payload_digest=_OTHER_DIGEST,
        now=100.0,
    ) is False


def test_two_connections_have_exactly_one_cancel_dispatch_winner(tmp_path):
    """Cross-process-shaped cancel claims permit one wire cancellation."""
    path = tmp_path / 'state.sqlite3'
    first = GazeboMonitorRoomStore(path)
    first.prepare(_request(), now=1.0)
    _lease(first, now=2.0)
    _to_navigating(first)
    first.request_cancel(_cancel_request(first), now=6.0)
    second = GazeboMonitorRoomStore(path)
    binding_digest = first.private_operation_binding(
        'operation-1'
    ).binding_digest
    barrier = Barrier(2)

    def claim(store):
        barrier.wait()
        return store.claim_cancel_dispatch(
            store.transition_token(
                'operation-1', worker_id='worker-1'
            ),
            cancel_request_id='cancel-1',
            request_fingerprint=_DIGEST,
            binding_digest=binding_digest,
            wire_payload_digest=_WIRE_DIGEST,
            now=7.0,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(claim, (first, second)))
    assert sorted(results) == [False, True]


def test_cancel_dispatch_assertion_is_exact_after_deadline_and_renewal(
    tmp_path,
):
    """Cancel proof ignores the operation deadline but needs a live lease."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(deadline=8.0), now=1.0)
    first_lease = store.acquire_lease(
        'operation-1',
        worker_id='worker-1',
        expected_fence=0,
        lease_seconds=8.0,
        now=2.0,
    )
    _to_navigating(store)
    request = _cancel_request(store)
    store.request_cancel(request, now=6.0)
    token = store.transition_token(
        'operation-1', worker_id='worker-1'
    )
    binding = store.private_operation_binding(
        'operation-1'
    ).binding_digest
    with pytest.raises(GazeboMonitorRoomConflictError):
        store.assert_cancel_dispatch_claim(
            token,
            cancel_request_id='cancel-1',
            request_fingerprint=_DIGEST,
            binding_digest=binding,
            wire_payload_digest=_WIRE_DIGEST,
            now=7.0,
        )
    assert store.claim_cancel_dispatch(
        token,
        cancel_request_id='cancel-1',
        request_fingerprint=_DIGEST,
        binding_digest=binding,
        wire_payload_digest=_WIRE_DIGEST,
        now=7.0,
    ) is True
    renewed = store.acquire_lease(
        'operation-1',
        worker_id='worker-1',
        expected_fence=first_lease.fence_epoch,
        lease_seconds=20.0,
        now=9.0,
    )
    store.close()

    reopened = GazeboMonitorRoomStore(path)
    token = reopened.transition_token(
        'operation-1', worker_id='worker-1'
    )
    evidence = reopened.assert_cancel_dispatch_claim(
        token,
        cancel_request_id='cancel-1',
        request_fingerprint=_DIGEST,
        binding_digest=binding,
        wire_payload_digest=_WIRE_DIGEST,
        now=11.0,
    )
    assert type(evidence) is DispatchClaimEvidence
    assert evidence.phase == 'cancel'
    assert evidence.operation_state == 'cancel_requested'
    assert evidence.sample_state == 'cancel_requested'
    assert evidence.start_fingerprint is None
    assert evidence.preflight_digest is None
    assert evidence.cancel_request_id == 'cancel-1'
    assert evidence.cancel_request_fingerprint == _DIGEST
    assert evidence.claim_lease_expires_at == first_lease.lease_expires_at
    assert evidence.current_lease_expires_at == renewed.lease_expires_at
    assert evidence.operation_deadline == 8.0
    assert evidence.checked_at == 11.0
    assert evidence.checked_at > evidence.operation_deadline
    assert evidence.checked_at > evidence.claim_lease_expires_at

    exact = {
        'cancel_request_id': 'cancel-1',
        'request_fingerprint': _DIGEST,
        'binding_digest': binding,
        'wire_payload_digest': _WIRE_DIGEST,
        'now': 12.0,
    }
    for field_name, value in (
        ('cancel_request_id', 'cancel-other'),
        ('request_fingerprint', _OTHER_DIGEST),
        ('binding_digest', _OTHER_DIGEST),
        ('wire_payload_digest', _OTHER_DIGEST),
    ):
        conflicting = dict(exact)
        conflicting[field_name] = value
        with pytest.raises(GazeboMonitorRoomConflictError) as captured:
            reopened.assert_cancel_dispatch_claim(
                token, **conflicting
            )
        assert value not in str(captured.value)
        assert captured.value.__cause__ is None
    with pytest.raises(GazeboMonitorRoomClockRollbackError):
        reopened.assert_cancel_dispatch_claim(
            token,
            cancel_request_id='cancel-1',
            request_fingerprint=_DIGEST,
            binding_digest=binding,
            wire_payload_digest=_WIRE_DIGEST,
            now=8.0,
        )
    with pytest.raises(GazeboMonitorRoomLeaseError):
        reopened.assert_cancel_dispatch_claim(
            token,
            cancel_request_id='cancel-1',
            request_fingerprint=_DIGEST,
            binding_digest=binding,
            wire_payload_digest=_WIRE_DIGEST,
            now=renewed.lease_expires_at,
        )


@pytest.mark.parametrize('phase', ('start', 'cancel'))
def test_dispatch_assertions_reject_takeover_but_claim_replay_is_false(
    tmp_path, phase
):
    """A new fence may observe a claim but can never use its authority."""
    store = GazeboMonitorRoomStore(tmp_path / f'{phase}.sqlite3')
    store.prepare(_request(), now=1.0)
    first_lease = _lease(store, now=2.0)
    binding = store.private_operation_binding(
        'operation-1'
    ).binding_digest
    if phase == 'start':
        _to_send_intent(store)
        assert _claim_start(store, now=5.0) is True
    else:
        _to_navigating(store)
        store.request_cancel(_cancel_request(store), now=6.0)
        assert store.claim_cancel_dispatch(
            store.transition_token(
                'operation-1', worker_id='worker-1'
            ),
            cancel_request_id='cancel-1',
            request_fingerprint=_DIGEST,
            binding_digest=binding,
            wire_payload_digest=_WIRE_DIGEST,
            now=7.0,
        ) is True
    old_token = store.transition_token(
        'operation-1', worker_id='worker-1'
    )
    takeover = store.acquire_lease(
        'operation-1',
        worker_id='worker-2',
        expected_fence=first_lease.fence_epoch,
        lease_seconds=20.0,
        now=first_lease.lease_expires_at,
    )
    new_token = store.transition_token(
        'operation-1', worker_id='worker-2'
    )
    if phase == 'start':
        assert store.claim_start_dispatch(
            new_token,
            start_fingerprint=_OTHER_DIGEST,
            binding_digest=_OTHER_DIGEST,
            preflight_digest=_OTHER_DIGEST,
            wire_payload_digest=_OTHER_DIGEST,
            now=takeover.observation.updated_at,
        ) is False

        def assertion(token):
            return store.assert_start_dispatch_claim(
                token,
                start_fingerprint=_DIGEST,
                binding_digest=binding,
                preflight_digest=_DIGEST,
                wire_payload_digest=_WIRE_DIGEST,
                now=takeover.observation.updated_at,
            )
    else:
        assert store.claim_cancel_dispatch(
            new_token,
            cancel_request_id='cancel-other',
            request_fingerprint=_OTHER_DIGEST,
            binding_digest=_OTHER_DIGEST,
            wire_payload_digest=_OTHER_DIGEST,
            now=takeover.observation.updated_at,
        ) is False

        def assertion(token):
            return store.assert_cancel_dispatch_claim(
                token,
                cancel_request_id='cancel-1',
                request_fingerprint=_DIGEST,
                binding_digest=binding,
                wire_payload_digest=_WIRE_DIGEST,
                now=takeover.observation.updated_at,
            )
    with pytest.raises(GazeboMonitorRoomFenceError):
        assertion(old_token)
    with pytest.raises(GazeboMonitorRoomConflictError):
        assertion(new_token)


def test_dispatch_claim_rows_are_append_only(tmp_path):
    """Direct update, delete, and replacement cannot erase first dispatch."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(), now=1.0)
    _lease(store, now=2.0)
    _to_send_intent(store)
    assert _claim_start(store, now=5.0) is True
    store.close()

    connection = sqlite3.connect(path)
    for statement in (
        'UPDATE gazebo_monitor_room_dispatch_claims '
        "SET worker_id = 'worker-2'",
        'DELETE FROM gazebo_monitor_room_dispatch_claims',
        'INSERT OR REPLACE INTO gazebo_monitor_room_dispatch_claims '
        'SELECT * FROM gazebo_monitor_room_dispatch_claims',
    ):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement)
    connection.close()


def test_rehashed_future_dispatch_claim_fails_chronology_on_reopen(
    tmp_path,
):
    """A valid-shape rehash cannot move a claim beyond lease/deadline."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(), now=1.0)
    _lease(store, now=2.0)
    _to_send_intent(store)
    assert _claim_start(store, now=5.0) is True
    store.close()

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        'DROP TRIGGER gazebo_monitor_room_dispatch_claim_no_update'
    )
    claim = dict(connection.execute(
        'SELECT * FROM gazebo_monitor_room_dispatch_claims'
    ).fetchone())
    claim['claimed_at'] = 99.0
    claim['lease_expires_at'] = 100.0
    claim['record_digest'] = _dispatch_claim_digest(claim)
    connection.execute(
        '''
        UPDATE gazebo_monitor_room_dispatch_claims
        SET claimed_at = ?, lease_expires_at = ?, record_digest = ?
        ''',
        (
            claim['claimed_at'],
            claim['lease_expires_at'],
            claim['record_digest'],
        ),
    )
    connection.execute(DISPATCH_CLAIM_NO_UPDATE_TRIGGER_SQL)
    connection.commit()
    connection.close()

    with pytest.raises(GazeboMonitorRoomSchemaError):
        GazeboMonitorRoomStore(path)


@pytest.mark.parametrize('phase', ['start', 'cancel'])
def test_claim_cannot_borrow_a_later_fence_lease_expiry(tmp_path, phase):
    """A claim stays inside its own fence interval after takeover."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(), now=1.0)
    first_lease = store.acquire_lease(
        'operation-1',
        worker_id='worker-1',
        expected_fence=0,
        lease_seconds=8.0,
        now=2.0,
    )
    if phase == 'start':
        _to_send_intent(store)
        assert _claim_start(store, now=7.0) is True
    else:
        _to_navigating(store)
        store.request_cancel(_cancel_request(store), now=6.0)
        token = store.transition_token(
            'operation-1', worker_id='worker-1'
        )
        binding = store.private_operation_binding(
            'operation-1'
        ).binding_digest
        assert store.claim_cancel_dispatch(
            token,
            cancel_request_id='cancel-1',
            request_fingerprint=_DIGEST,
            binding_digest=binding,
            wire_payload_digest=_WIRE_DIGEST,
            now=7.0,
        ) is True
    takeover = store.acquire_lease(
        'operation-1',
        worker_id='worker-2',
        expected_fence=first_lease.fence_epoch,
        lease_seconds=10.0,
        now=first_lease.lease_expires_at,
    )
    store.close()

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        'DROP TRIGGER gazebo_monitor_room_dispatch_claim_no_update'
    )
    claim = dict(connection.execute(
        'SELECT * FROM gazebo_monitor_room_dispatch_claims'
    ).fetchone())
    claim['claimed_at'] = first_lease.lease_expires_at + 1.0
    claim['lease_expires_at'] = takeover.lease_expires_at
    claim['record_digest'] = _dispatch_claim_digest(claim)
    connection.execute(
        '''
        UPDATE gazebo_monitor_room_dispatch_claims
        SET claimed_at = ?, lease_expires_at = ?, record_digest = ?
        ''',
        (
            claim['claimed_at'],
            claim['lease_expires_at'],
            claim['record_digest'],
        ),
    )
    connection.execute(DISPATCH_CLAIM_NO_UPDATE_TRIGGER_SQL)
    connection.commit()
    connection.close()

    with pytest.raises(GazeboMonitorRoomSchemaError):
        GazeboMonitorRoomStore(path)


def test_delivery_unknown_is_durable_and_blocks_new_robot_work(tmp_path):
    """Forgotten Nav2 identity is never rewritten as success or retried."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(), now=1.0)
    _lease(store)
    _to_navigating(store)
    unknown = store.record_delivery_unknown(
        store.transition_token('operation-1', worker_id='worker-1'),
        code='nav2_goal_not_observable',
        evidence_digest=_DIGEST,
        now=6.0,
    )
    assert unknown.state == 'delivery_unknown'
    assert unknown.terminal is True
    assert unknown.robot_blocked is True
    store.close()

    reopened = GazeboMonitorRoomStore(path)
    assert reopened.observe('operation-1').state == 'delivery_unknown'
    with pytest.raises(GazeboMonitorRoomConflictError):
        reopened.prepare(
            _request(
                operation_id='operation-2',
                prepare_request_id='prepare-2',
            ),
            now=7.0,
        )


def test_sent_goal_failure_requires_terminal_evidence(tmp_path):
    """A sent or active goal cannot be called failed without evidence."""
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(_request(), now=1.0)
    _lease(store)
    _to_navigating(store)
    token = store.transition_token('operation-1', worker_id='worker-1')
    with pytest.raises(GazeboMonitorRoomValidationError):
        store.record_failed(
            token,
            code='nav2_goal_aborted',
            evidence_digest=None,
            now=6.0,
        )
    failed = store.record_failed(
        token,
        code='nav2_goal_aborted',
        evidence_digest=_DIGEST,
        now=6.0,
    )
    assert failed.state == 'failed'


def test_cancel_before_send_is_exact_and_needs_no_nav2_evidence(tmp_path):
    """Pre-send cancellation can become canceled without a Nav2 ACK."""
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(_request(), now=1.0)
    _lease(store)
    request = _cancel_request(store)
    pending = store.request_cancel(request, now=3.0)
    assert pending.state == 'cancel_requested'
    replay = store.request_cancel(request, now=4.0)
    assert replay.replayed is True
    with pytest.raises(GazeboMonitorRoomConflictError):
        store.request_cancel(
            replace(request, reason_code='deadline_expired'), now=4.0
        )
    canceled = store.record_canceled(
        store.transition_token('operation-1', worker_id='worker-1'),
        terminal_evidence_digest=None,
        now=5.0,
    )
    assert canceled.state == 'canceled'


def test_active_cancel_requires_terminal_evidence_and_beats_late_success(
    tmp_path,
):
    """Cancel intent wins the serialized CAS and a late success is stale."""
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(_request(), now=1.0)
    _lease(store)
    _to_navigating(store)
    success_token = store.transition_token(
        'operation-1', worker_id='worker-1'
    )
    store.request_cancel(_cancel_request(store), now=6.0)
    with pytest.raises(GazeboMonitorRoomConflictError):
        store.record_sample_succeeded(
            success_token,
            result_evidence_digest=_DIGEST,
            now=7.0,
        )
    with pytest.raises(GazeboMonitorRoomValidationError):
        store.record_canceled(
            store.transition_token('operation-1', worker_id='worker-1'),
            terminal_evidence_digest=None,
            now=7.0,
        )
    canceled = store.record_canceled(
        store.transition_token('operation-1', worker_id='worker-1'),
        terminal_evidence_digest=_DIGEST,
        now=7.0,
    )
    assert canceled.state == 'canceled'


def test_success_wins_cancel_race_without_cancel_claim(tmp_path):
    """A cancel arriving after durable success observes that terminal state."""
    one_sample = (OrderedSemanticSample(0, 0, 0, 1000, 2000),)
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(_request(samples=one_sample), now=1.0)
    _lease(store)
    _to_navigating(store)
    cancel = _cancel_request(store)
    succeeded = store.record_sample_succeeded(
        store.transition_token('operation-1', worker_id='worker-1'),
        result_evidence_digest=_DIGEST,
        now=6.0,
    )
    assert succeeded.state == 'succeeded'
    observed = store.request_cancel(cancel, now=7.0)
    assert observed.state == 'succeeded'
    assert observed.cancel_request_id is None


def test_cancel_unknown_stays_blocking_across_restart(tmp_path):
    """A cancellation ACK without terminal result cannot unblock the robot."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(), now=1.0)
    _lease(store)
    _to_navigating(store)
    store.request_cancel(_cancel_request(store), now=6.0)
    unknown = store.record_cancel_unknown(
        store.transition_token('operation-1', worker_id='worker-1'),
        code='nav2_cancel_terminal_not_observable',
        evidence_digest=_DIGEST,
        now=7.0,
    )
    assert unknown.state == 'cancel_unknown'
    assert unknown.robot_blocked is True
    store.close()
    assert GazeboMonitorRoomStore(path).observe(
        'operation-1'
    ).robot_blocked is True


def test_deadline_stops_new_send_and_fails_before_next_sample(tmp_path):
    """Deadline expiry permits evidence recording but no next side effect."""
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    with pytest.raises(GazeboMonitorRoomDeadlineError):
        store.prepare(_request(deadline=1.0), now=1.0)

    store.prepare(_request(deadline=6.0), now=1.0)
    _lease(store)
    store.begin_preflight(
        store.transition_token('operation-1', worker_id='worker-1'),
        now=3.0,
    )
    store.record_send_intent(
        store.transition_token('operation-1', worker_id='worker-1'),
        preflight_digest=_DIGEST,
        now=4.0,
    )
    store.record_navigating(
        store.transition_token('operation-1', worker_id='worker-1'),
        acceptance_digest=_DIGEST,
        now=5.0,
    )
    expired = store.record_sample_succeeded(
        store.transition_token('operation-1', worker_id='worker-1'),
        result_evidence_digest=_DIGEST,
        now=6.0,
    )
    assert expired.state == 'failed'
    assert expired.terminal_code == 'deadline_expired'
    assert expired.navigation_samples_reached == 1
    assert expired.current_sample_state == 'failed'


def test_exact_schema_and_append_only_triggers_reject_mutation(tmp_path):
    """Direct replacement, deletion, and event edits are rejected."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(), now=1.0)
    store.close()
    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            '''
            DELETE FROM gazebo_monitor_room_events
            WHERE operation_id = 'operation-1' AND event_seq = 1
            '''
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            '''
            UPDATE gazebo_monitor_room_samples
            SET x_mm = 999
            WHERE operation_id = 'operation-1' AND sample_index = 0
            '''
        )
    connection.close()


def test_restored_trigger_tamper_fails_closed_on_reopen(tmp_path):
    """Restoring exact trigger SQL cannot hide a modified durable row."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(), now=1.0)
    store.close()

    connection = sqlite3.connect(path)
    connection.execute('DROP TRIGGER gazebo_monitor_room_operation_identity')
    connection.execute(
        '''
        UPDATE gazebo_monitor_room_operations
        SET map_revision = 'forged-revision'
        WHERE operation_id = 'operation-1'
        '''
    )
    connection.execute(OPERATION_IDENTITY_TRIGGER_SQL)
    connection.commit()
    connection.close()

    with pytest.raises(GazeboMonitorRoomSchemaError):
        GazeboMonitorRoomStore(path)


def test_reopen_recomputes_prepare_binding_beyond_row_and_event_digests(
    tmp_path,
):
    """A forged but internally rehashed prepare fingerprint fails closed."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(), now=1.0)
    store.close()

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute('DROP TRIGGER gazebo_monitor_room_operation_identity')
    connection.execute(
        'DROP TRIGGER gazebo_monitor_room_operation_transition'
    )
    connection.execute('DROP TRIGGER gazebo_monitor_room_event_no_update')
    operation = dict(connection.execute(
        '''
        SELECT * FROM gazebo_monitor_room_operations
        WHERE operation_id = 'operation-1'
        '''
    ).fetchone())
    operation['prepare_fingerprint'] = _OTHER_DIGEST
    operation['record_digest'] = _operation_digest(operation)
    connection.execute(
        '''
        UPDATE gazebo_monitor_room_operations
        SET prepare_fingerprint = ?, record_digest = ?
        WHERE operation_id = 'operation-1'
        ''',
        (
            operation['prepare_fingerprint'],
            operation['record_digest'],
        ),
    )
    previous_digest = '0' * 64
    events = connection.execute(
        '''
        SELECT * FROM gazebo_monitor_room_events
        WHERE operation_id = 'operation-1' ORDER BY event_seq
        '''
    ).fetchall()
    for stored in events:
        event = dict(stored)
        event['operation_record_digest'] = operation['record_digest']
        event['previous_event_digest'] = previous_digest
        event['event_digest'] = _event_digest(event)
        connection.execute(
            '''
            UPDATE gazebo_monitor_room_events
            SET operation_record_digest = ?, previous_event_digest = ?,
                event_digest = ?
            WHERE operation_id = 'operation-1' AND event_seq = ?
            ''',
            (
                event['operation_record_digest'],
                event['previous_event_digest'],
                event['event_digest'],
                event['event_seq'],
            ),
        )
        previous_digest = event['event_digest']
    connection.execute(OPERATION_IDENTITY_TRIGGER_SQL)
    connection.execute(OPERATION_TRANSITION_TRIGGER_SQL)
    connection.execute(EVENT_NO_UPDATE_TRIGGER_SQL)
    connection.commit()
    connection.close()

    with pytest.raises(GazeboMonitorRoomSchemaError):
        GazeboMonitorRoomStore(path)


def test_missing_or_weakened_trigger_fails_closed_on_reopen(tmp_path):
    """The store authenticates exact trigger text, not only trigger names."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(), now=1.0)
    store.close()
    connection = sqlite3.connect(path)
    connection.execute('DROP TRIGGER gazebo_monitor_room_event_no_delete')
    connection.execute(
        '''
        CREATE TRIGGER gazebo_monitor_room_event_no_delete
        BEFORE DELETE ON gazebo_monitor_room_events BEGIN SELECT 1; END
        '''
    )
    connection.commit()
    connection.close()
    with pytest.raises(GazeboMonitorRoomSchemaError):
        GazeboMonitorRoomStore(path)


def test_unexpected_owned_schema_object_fails_closed(tmp_path):
    """A similarly prefixed table cannot hide beside the exact schema."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.close()
    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE gazebo_monitor_room_shadow (x INT)')
    connection.commit()
    connection.close()
    with pytest.raises(GazeboMonitorRoomSchemaError):
        GazeboMonitorRoomStore(path)


def test_existing_database_with_all_owned_objects_removed_fails_closed(
    tmp_path,
):
    """A nonempty prior DB can never be mistaken for a brand-new DB."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(), now=1.0)
    store.close()

    connection = sqlite3.connect(path)
    connection.execute('PRAGMA foreign_keys = OFF')
    for table in (
        'gazebo_monitor_room_events',
        'gazebo_monitor_room_samples',
        'gazebo_monitor_room_operations',
        'gazebo_monitor_room_schema_metadata',
    ):
        connection.execute(f'DROP TABLE {table}')
    connection.commit()
    connection.close()
    assert path.stat().st_size > 0
    with pytest.raises(GazeboMonitorRoomSchemaError):
        GazeboMonitorRoomStore(path)


def test_database_file_is_private(tmp_path):
    """A newly opened coordinate-bearing database is mode 0600."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(path)
    try:
        assert os.stat(path).st_mode & 0o777 == 0o600
    finally:
        store.close()


def test_database_creation_is_private_even_with_permissive_umask(tmp_path):
    """Coordinates are never exposed before a post-insert chmod."""
    path = tmp_path / 'state.sqlite3'
    previous = os.umask(0)
    try:
        store = GazeboMonitorRoomStore(path)
    finally:
        os.umask(previous)
    try:
        assert os.stat(path).st_mode & 0o777 == 0o600
        store.prepare(_request(), now=1.0)
    finally:
        store.close()


def test_database_rejects_symlink_hardlink_and_wrong_mode(tmp_path):
    """An existing private DB path must have one trusted regular-file link."""
    target = tmp_path / 'target.sqlite3'
    target.touch(mode=0o600)
    symlink = tmp_path / 'symlink.sqlite3'
    symlink.symlink_to(target)
    with pytest.raises(GazeboMonitorRoomValidationError):
        GazeboMonitorRoomStore(symlink)

    hardlink = tmp_path / 'hardlink.sqlite3'
    os.link(target, hardlink)
    with pytest.raises(GazeboMonitorRoomValidationError):
        GazeboMonitorRoomStore(hardlink)
    hardlink.unlink()
    target.chmod(0o640)
    with pytest.raises(GazeboMonitorRoomValidationError):
        GazeboMonitorRoomStore(target)


def test_open_store_rejects_unlinked_or_replaced_database_path(tmp_path):
    """An open connection must not keep serving an orphaned DB inode."""
    path = tmp_path / 'unlinked.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(), now=1.0)

    path.unlink()
    with pytest.raises(GazeboMonitorRoomDurabilityError) as missing:
        store.observe('operation-1')
    assert not isinstance(missing.value, sqlite3.Error)

    path = tmp_path / 'replaced.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(), now=1.0)
    path.unlink()
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    with pytest.raises(GazeboMonitorRoomDurabilityError) as replaced:
        store.acquire_lease(
            'operation-1',
            worker_id='worker-1',
            expected_fence=0,
            lease_seconds=10.0,
            now=2.0,
        )
    assert not isinstance(replaced.value, sqlite3.Error)


def test_open_store_rejects_database_mode_or_link_count_drift(tmp_path):
    """Each transaction revalidates the still-open DB path metadata."""
    path = tmp_path / 'hardlinked.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(), now=1.0)

    hardlink = tmp_path / 'state-alias.sqlite3'
    os.link(path, hardlink)
    with pytest.raises(GazeboMonitorRoomDurabilityError):
        store.observe('operation-1')
    hardlink.unlink()

    path = tmp_path / 'wrong-mode.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(), now=1.0)
    path.chmod(0o640)
    with pytest.raises(GazeboMonitorRoomDurabilityError):
        store.observe('operation-1')


@pytest.mark.parametrize('transaction_kind', ('read', 'write'))
def test_transaction_rechecks_path_after_commit(
    tmp_path, monkeypatch, transaction_kind
):
    """A path loss after the pre-commit check never returns success."""
    path = tmp_path / f'{transaction_kind}.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(), now=1.0)
    precommit_attested = Barrier(2)
    path_removed = Barrier(2)
    calls = 0
    original_attest = store._attest_database_locked

    def pause_after_precommit_attestation(connection):
        nonlocal calls
        calls += 1
        original_attest(connection)
        if calls == 3:
            precommit_attested.wait(timeout=5.0)
            path_removed.wait(timeout=5.0)

    monkeypatch.setattr(
        store,
        '_attest_database_locked',
        pause_after_precommit_attestation,
    )

    def transact():
        if transaction_kind == 'read':
            return store.observe('operation-1')
        return store.acquire_lease(
            'operation-1',
            worker_id='worker-1',
            expected_fence=0,
            lease_seconds=10.0,
            now=2.0,
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(transact)
        precommit_attested.wait(timeout=5.0)
        path.unlink()
        path_removed.wait(timeout=5.0)
        with pytest.raises(GazeboMonitorRoomDurabilityError):
            result.result(timeout=5.0)
    assert calls == 4
    with pytest.raises(GazeboMonitorRoomStoreError):
        store.observe('operation-1')


@pytest.mark.parametrize(
    ('pragma_name', 'drift_value'),
    (
        ('foreign_keys', 'OFF'),
        ('recursive_triggers', 'OFF'),
        ('trusted_schema', 'ON'),
        ('query_only', 'ON'),
        ('synchronous', 'NORMAL'),
        ('journal_mode', 'MEMORY'),
    ),
)
def test_transaction_rejects_sqlite_safety_pragma_drift(
    tmp_path, pragma_name, drift_value
):
    """Connection-local safety configuration is attested every time."""
    store = GazeboMonitorRoomStore(
        tmp_path / f'{pragma_name}.sqlite3'
    )
    store.prepare(_request(), now=1.0)
    connection = store._connection
    assert connection is not None
    connection.execute(f'PRAGMA {pragma_name} = {drift_value}')

    with pytest.raises(GazeboMonitorRoomDurabilityError):
        store.observe('operation-1')
    with pytest.raises(GazeboMonitorRoomStoreError):
        store.observe('operation-1')


@pytest.mark.parametrize('transaction_kind', ('read', 'write'))
def test_transaction_rejects_live_connection_path_split(
    tmp_path, transaction_kind
):
    """A connection for another valid DB cannot serve this store path."""
    first = GazeboMonitorRoomStore(tmp_path / 'first.sqlite3')
    second = GazeboMonitorRoomStore(tmp_path / 'second.sqlite3')
    first.prepare(_request(), now=1.0)
    second.prepare(_request(), now=1.0)
    first_connection = first._connection
    second_connection = second._connection
    assert first_connection is not None
    assert second_connection is not None
    first._connection = second_connection
    second._connection = first_connection
    try:
        with pytest.raises(GazeboMonitorRoomDurabilityError):
            if transaction_kind == 'read':
                first.observe('operation-1')
            else:
                first.acquire_lease(
                    'operation-1',
                    worker_id='worker-1',
                    expected_fence=0,
                    lease_seconds=10.0,
                    now=2.0,
                )
    finally:
        first.close()
        second.close()


def test_preexisting_empty_database_is_not_silently_initialized(tmp_path):
    """A truncated durable path is not distinguishable from an empty file."""
    path = tmp_path / 'state.sqlite3'
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    with pytest.raises(GazeboMonitorRoomSchemaError):
        GazeboMonitorRoomStore(path)


def test_database_rejects_unprotected_or_symlink_parent(tmp_path):
    """The final path requires a service-owned non-writable real parent."""
    unprotected = tmp_path / 'unprotected'
    unprotected.mkdir(mode=0o777)
    unprotected.chmod(0o777)
    with pytest.raises(GazeboMonitorRoomValidationError):
        GazeboMonitorRoomStore(unprotected / 'state.sqlite3')

    protected = tmp_path / 'protected'
    protected.mkdir(mode=0o700)
    linked_parent = tmp_path / 'linked-parent'
    linked_parent.symlink_to(protected, target_is_directory=True)
    with pytest.raises(GazeboMonitorRoomValidationError):
        GazeboMonitorRoomStore(linked_parent / 'state.sqlite3')

    child = protected / 'child'
    child.mkdir(mode=0o700)
    linked_ancestor = tmp_path / 'linked-ancestor'
    linked_ancestor.symlink_to(protected, target_is_directory=True)
    with pytest.raises(GazeboMonitorRoomValidationError):
        GazeboMonitorRoomStore(
            linked_ancestor / 'child' / 'state.sqlite3'
        )


def test_database_rejects_unprotected_intermediate_parent(tmp_path):
    """Every directory component must be protected, not only the final one."""
    unprotected = tmp_path / 'unprotected-ancestor'
    unprotected.mkdir(mode=0o777)
    unprotected.chmod(0o777)
    child = unprotected / 'protected-child'
    child.mkdir(mode=0o700)

    with pytest.raises(GazeboMonitorRoomValidationError):
        GazeboMonitorRoomStore(child / 'state.sqlite3')


def test_same_store_supports_parallel_observers(tmp_path):
    """ROS-style worker threads can share one validated store instance."""
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(_request(), now=1.0)
    barrier = Barrier(8)

    def observe_once(_index):
        barrier.wait()
        return store.observe('operation-1')

    with ThreadPoolExecutor(max_workers=8) as executor:
        observations = list(executor.map(observe_once, range(8)))
    assert len(observations) == 8
    assert {value.state for value in observations} == {'prepared'}


def test_same_store_serializes_parallel_lease_acquisition(tmp_path):
    """One of eight competing fence-zero claims wins without raw DB errors."""
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(_request(), now=1.0)
    barrier = Barrier(8)

    def acquire(index):
        barrier.wait()
        try:
            return store.acquire_lease(
                'operation-1',
                worker_id=f'worker-{index}',
                expected_fence=0,
                lease_seconds=10.0,
                now=2.0,
            )
        except GazeboMonitorRoomConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(acquire, range(8)))
    grants = [value for value in outcomes if not isinstance(value, Exception)]
    conflicts = [value for value in outcomes if isinstance(value, Exception)]
    assert len(grants) == 1
    assert len(conflicts) == 7
    assert all(
        isinstance(value, GazeboMonitorRoomFenceError)
        for value in conflicts
    )


def test_close_race_returns_values_or_typed_closed_store_error(tmp_path):
    """Close waits for a transaction and never leaks sqlite thread errors."""
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(_request(), now=1.0)
    barrier = Barrier(9)

    def observe_until_closed(_index):
        barrier.wait()
        values = []
        for _attempt in range(20):
            try:
                values.append(store.observe('operation-1').state)
            except GazeboMonitorRoomStoreError as error:
                values.append(type(error).__name__)
                break
        return values

    def close_store():
        barrier.wait()
        store.close()
        return 'closed'

    with ThreadPoolExecutor(max_workers=9) as executor:
        observers = [
            executor.submit(observe_until_closed, index)
            for index in range(8)
        ]
        closer = executor.submit(close_store)
        results = [future.result() for future in observers]
        assert closer.result() == 'closed'
    assert all(values for values in results)
    assert all(
        set(values) <= {'prepared', 'GazeboMonitorRoomStoreError'}
        for values in results
    )


def test_database_path_must_be_absolute_and_normalized(tmp_path, monkeypatch):
    """A durable coordinate store is never opened through cwd or '..'."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(GazeboMonitorRoomValidationError):
        GazeboMonitorRoomStore('relative-state.sqlite3')
    with pytest.raises(GazeboMonitorRoomValidationError):
        GazeboMonitorRoomStore(tmp_path / 'child' / '..' / 'state.sqlite3')
