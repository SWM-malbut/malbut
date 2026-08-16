"""Tests for the read-only ROS-to-RobotState observation boundary."""

import ast
import inspect
import os
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from malbut_agent_server.robot_state import (
    TrustedRobotStateError,
    UnixSocketTrustedRobotStateSource,
    parse_trusted_robot_state_envelope,
    trusted_boottime_ns,
)
from malbut_agent_server.robot_state_collector import (
    RobotStateCollectorError,
    RobotStateSnapshotStore,
)
from malbut_gazebo import robot_state_observer as observer_module
from malbut_gazebo.robot_state_observation import (
    HOMECAM_MEDIA_EVIDENCE_TOPIC,
    HOMECAM_STATE_FALSE,
    HOMECAM_STATE_TRUE,
    HOMECAM_STATE_UNKNOWN,
    HomecamMediaEvidenceTracker,
    HomecamMediaObservationPublisher,
    Nav2ObservationBatch,
    RobotStateObservationPublisher,
    TimedBoolObservation,
)
from malbut_gazebo.robot_state_observer import (
    RobotStateObserverConfig,
    _fresh_tf_observation,
    _lifecycle_observation,
)


_BOOT_ID = '11111111-1111-4111-8111-111111111111'
_INSTANCE_ID = '22222222-2222-4222-8222-222222222222'
_MEDIA_SOURCE_A = '33333333-3333-4333-8333-333333333333'
_MEDIA_SOURCE_B = '44444444-4444-4444-8444-444444444444'
_MEDIA_SOURCE_C = '55555555-5555-4555-8555-555555555555'
_MEDIA_SESSION = '66666666-6666-4666-8666-666666666666'
_NOW_NS = 1_000_000_000_000
_NONCE = 'a' * 64
_UTC_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self, value=_NOW_NS):
        self.value = value

    def __call__(self):
        return self.value


def _store(clock, *, authority=True):
    return RobotStateSnapshotStore._for_test(
        'malbut-device-01',
        'home-map',
        'grid-7',
        ttl_seconds=1.0,
        physical_authority=authority,
        host_boot_id=_BOOT_ID,
        instance_id=_INSTANCE_ID,
        boottime_ns=clock,
        utc_now=lambda: _UTC_NOW,
    )


def _known(value, receipt=_NOW_NS):
    return TimedBoolObservation(value, receipt)


def _batch(store, **changes):
    values = {
        'binding_token': store.binding_token(),
        'amcl_active': _known(True),
        'bt_navigator_active': _known(True),
        'planner_server_active': _known(True),
        'controller_server_active': _known(True),
        'global_costmap_active': _known(True),
        'compute_path_ready': _known(True),
        'navigate_ready': _known(True),
        'global_costmap_ready': _known(True),
        'map_tf_fresh': _known(True),
    }
    values.update(changes)
    return Nav2ObservationBatch(**values)


def _media_message(**changes):
    values = {
        'schema_version': 1,
        'device_id': 'malbut-device-01',
        'source_instance_id': _MEDIA_SOURCE_A,
        'sequence': 1,
        'control_plane_generation': 1,
        'observed_boottime_ns': _NOW_NS - 100_000_000,
        'valid_until_boottime_ns': _NOW_NS + 500_000_000,
        'camera_available_state': HOMECAM_STATE_TRUE,
        'privacy_mode_state': HOMECAM_STATE_FALSE,
        'last_valid_frame_boottime_ns': _NOW_NS - 150_000_000,
        'frame_generation': 1,
        'active_session_id': '',
        'active_session_generation': 0,
        'backend_device_bound': True,
        'physical_authority': True,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _media_tracker(*, authority=True, lifetime=1.0):
    return HomecamMediaEvidenceTracker(
        'malbut-device-01',
        require_physical_authority=authority,
        maximum_lifetime_seconds=lifetime,
    )


def _parse(store, clock):
    return parse_trusted_robot_state_envelope(
        store.snapshot(_NONCE),
        expected_nonce=_NONCE,
        expected_device_id=store.device_id,
        expected_host_boot_id=store.host_boot_id,
        now_boottime_ns=clock.value,
    )


def _transform(*, stamp_ns=_NOW_NS, x=1.0, quaternion=None):
    if quaternion is None:
        quaternion = (0.0, 0.0, 0.0, 1.0)
    return SimpleNamespace(
        header=SimpleNamespace(
            stamp=SimpleNamespace(
                sec=stamp_ns // 1_000_000_000,
                nanosec=stamp_ns % 1_000_000_000,
            )
        ),
        transform=SimpleNamespace(
            translation=SimpleNamespace(x=x, y=2.0, z=0.0),
            rotation=SimpleNamespace(
                x=quaternion[0],
                y=quaternion[1],
                z=quaternion[2],
                w=quaternion[3],
            ),
        ),
    )


def test_config_is_fail_closed_for_simulated_physical_authority() -> None:
    """Physical evidence must never be activated on ROS simulated time."""
    with pytest.raises(ValueError, match='simulated time'):
        RobotStateObserverConfig(
            device_id='malbut-device-01',
            map_id='home-map',
            map_revision='grid-7',
            socket_path='/run/malbut/robot-state.sock',
            expected_agent_uid=1000,
            physical_authority=True,
            use_sim_time=True,
        )


@pytest.mark.parametrize(
    ('changes', 'message'),
    [
        ({'observation_timeout_seconds': 1.1}, 'evidence TTL'),
        ({'lifecycle_poll_seconds': 0.8}, 'observation timeout'),
        ({'tf_max_age_seconds': 0.8}, 'observation timeout'),
        ({'expected_agent_uid': True}, 'expected_agent_uid'),
    ],
)
def test_config_rejects_freshness_and_type_mismatches(
    changes,
    message,
) -> None:
    """Configuration cannot silently extend evidence or coerce booleans."""
    values = {
        'device_id': 'malbut-device-01',
        'map_id': 'home-map',
        'map_revision': 'grid-7',
        'socket_path': '/run/malbut/robot-state.sock',
        'expected_agent_uid': 1000,
    }
    values.update(changes)
    with pytest.raises(ValueError, match=message):
        RobotStateObserverConfig(**values)


def test_lifecycle_primary_state_is_strict_tri_state() -> None:
    """Only a current active state grants positive Nav2 evidence."""
    assert _lifecycle_observation(3, _NOW_NS).value is True
    assert _lifecycle_observation(2, _NOW_NS).value is False
    assert _lifecycle_observation(0, _NOW_NS).value is None
    assert _lifecycle_observation(True, _NOW_NS).value is None


def test_lifecycle_callback_does_not_coerce_invalid_state_ids() -> None:
    """A truthy alias from a malformed response stays unknown."""
    future = SimpleNamespace(
        result=lambda: SimpleNamespace(
            current_state=SimpleNamespace(id=True),
        )
    )
    holder = SimpleNamespace(
        _state_lock=threading.RLock(),
        _lifecycle_futures={'amcl': future},
        _lifecycle={'amcl': _known(False)},
    )

    observer_module.TrustedRobotStateObserver._lifecycle_result(
        holder,
        'amcl',
        future,
    )

    assert holder._lifecycle['amcl'].value is None


def test_tf_requires_finite_nonfuture_fresh_transform() -> None:
    """A missing, malformed, future, or stale TF never looks localized."""
    fresh = _fresh_tf_observation(
        _transform(stamp_ns=_NOW_NS - 100_000_000),
        ros_now_ns=_NOW_NS,
        received_boottime_ns=_NOW_NS,
        max_age_seconds=0.5,
    )
    assert fresh.value is True
    invalid_nanoseconds = _transform(stamp_ns=0)
    invalid_nanoseconds.header.stamp.nanosec = 1_000_000_000
    coerced_seconds = _transform(stamp_ns=0)
    coerced_seconds.header.stamp.sec = True
    for transform in (
        _transform(stamp_ns=_NOW_NS + 1),
        _transform(stamp_ns=_NOW_NS - 500_000_000),
        _transform(x=float('nan')),
        _transform(quaternion=(0.0, 0.0, 0.0, 0.0)),
        _transform(quaternion=(2.0, 0.0, 0.0, 0.0)),
        invalid_nanoseconds,
        coerced_seconds,
    ):
        result = _fresh_tf_observation(
            transform,
            ros_now_ns=_NOW_NS,
            received_boottime_ns=_NOW_NS,
            max_age_seconds=0.5,
        )
        assert result.value is None


def test_all_read_only_observations_publish_one_atomic_sequence() -> None:
    """Nav2 and localization become known together, without other fields."""
    clock = _Clock()
    store = _store(clock)
    publisher = RobotStateObservationPublisher._for_test(
        store,
        observation_timeout_seconds=0.75,
        boottime_ns=clock,
    )
    assert publisher.publish(_batch(store)) == 1
    evidence = _parse(store, clock)
    assert evidence.sequence == 1
    assert evidence.navigation_available is True
    assert evidence.localization_ok is True
    assert evidence.battery_percent is None
    assert evidence.emergency_stop is None
    assert evidence.camera_available is None
    assert evidence.privacy_mode is None
    assert evidence.docked is None
    assert evidence.forbidden_zones is None
    with pytest.raises(
        TrustedRobotStateError,
        match='trusted robot state is unavailable',
    ):
        evidence.require_complete_for_monitor_room(clock.value)


def test_unknown_stale_and_explicit_inactive_remain_distinct() -> None:
    """Stale inputs clear to unknown while current inactive is false."""
    clock = _Clock()
    store = _store(clock)
    publisher = RobotStateObservationPublisher._for_test(
        store,
        observation_timeout_seconds=0.75,
        boottime_ns=clock,
    )
    publisher.publish(_batch(store))
    clock.value += 750_000_000
    publisher.publish(_batch(store))
    stale = _parse(store, clock)
    assert stale.navigation_available is None
    assert stale.localization_ok is None

    clock.value += 1
    current = _known(False, clock.value)
    inactive = replace(
        Nav2ObservationBatch.unknown(store.binding_token()),
        amcl_active=current,
    )
    publisher.publish(inactive)
    blocked = _parse(store, clock)
    assert blocked.navigation_available is False
    assert blocked.localization_ok is False


def test_stale_map_token_is_checked_even_for_deduplicated_batch() -> None:
    """A no-op cache must not bypass the collector's map generation CAS."""
    clock = _Clock()
    store = _store(clock)
    publisher = RobotStateObservationPublisher._for_test(
        store,
        observation_timeout_seconds=0.75,
        boottime_ns=clock,
    )
    old_batch = _batch(store)
    publisher.publish(old_batch)
    clock.value += 1
    store.update_binding('second-map', 'grid-8')
    with pytest.raises(RobotStateCollectorError) as captured:
        publisher.publish(old_batch)
    assert captured.value.code == 'robot_state_collector_binding_mismatch'


def test_nav2_field_lifetime_ends_at_observation_boundary() -> None:
    """Collector validity must not outlive the adapter observation timeout."""
    clock = _Clock()
    store = _store(clock)
    publisher = RobotStateObservationPublisher._for_test(
        store,
        observation_timeout_seconds=0.75,
        boottime_ns=clock,
    )
    publisher.publish(_batch(store))
    evidence = _parse(store, clock)
    assert evidence.valid_until_boottime_ns == _NOW_NS + 750_000_000
    clock.value = _NOW_NS + 750_000_000
    with pytest.raises(TrustedRobotStateError) as captured:
        _parse(store, clock)
    assert captured.value.code == 'robot_state_stale'


def test_exact_homecam_evidence_publishes_both_fields_atomically() -> None:
    """One valid device-bound frame publishes camera and privacy together."""
    clock = _Clock()
    store = _store(clock)
    tracker = _media_tracker()
    batch = tracker.observe(
        _media_message(),
        binding_token=store.binding_token(),
        received_boottime_ns=clock.value,
    )
    assert batch is not None
    assert batch.camera_available is True
    assert batch.privacy_mode is False
    assert batch.valid_for_ns == 500_000_000
    sequence = HomecamMediaObservationPublisher(store).publish(batch)
    assert sequence == 1
    evidence = _parse(store, clock)
    assert evidence.camera_available is True
    assert evidence.privacy_mode is False
    assert evidence.valid_until_boottime_ns == _NOW_NS + 500_000_000
    assert (
        evidence.field_evidence['camera_available'].received_boottime_ns
        == _NOW_NS
    )
    assert (
        evidence.field_evidence['privacy_mode'].received_boottime_ns
        == _NOW_NS
    )


def test_homecam_wire_lifetime_is_capped_by_collector_configuration() -> None:
    """A longer valid wire claim cannot extend the local collector TTL."""
    tracker = _media_tracker(lifetime=0.75)
    message = _media_message(
        valid_until_boottime_ns=_NOW_NS + 2_000_000_000,
    )
    store = _store(_Clock())
    batch = tracker.observe(
        message,
        binding_token=store.binding_token(),
        received_boottime_ns=_NOW_NS,
    )
    assert batch is not None
    assert batch.valid_for_ns == 750_000_000


@pytest.mark.parametrize(
    ('changes', 'receipt'),
    [
        ({'schema_version': True}, _NOW_NS),
        ({'sequence': True}, _NOW_NS),
        ({'control_plane_generation': True}, _NOW_NS),
        ({'camera_available_state': True}, _NOW_NS),
        ({'privacy_mode_state': True}, _NOW_NS),
        ({'backend_device_bound': 1}, _NOW_NS),
        ({'physical_authority': 1}, _NOW_NS),
        ({'device_id': 'another-device'}, _NOW_NS),
        ({'source_instance_id': '{' + _MEDIA_SOURCE_A + '}'}, _NOW_NS),
        ({'observed_boottime_ns': _NOW_NS + 1}, _NOW_NS),
        ({'valid_until_boottime_ns': _NOW_NS}, _NOW_NS),
        (
            {
                'observed_boottime_ns': _NOW_NS - 5_000_000_000,
                'valid_until_boottime_ns': _NOW_NS + 1,
            },
            _NOW_NS,
        ),
    ],
)
def test_homecam_malformed_mismatched_and_stale_values_clear_atomically(
    changes,
    receipt,
) -> None:
    """Malformed identity, type, and clock claims never become known."""
    store = _store(_Clock())
    batch = _media_tracker().observe(
        _media_message(**changes),
        binding_token=store.binding_token(),
        received_boottime_ns=receipt,
    )
    assert batch is not None
    assert batch.camera_available is None
    assert batch.privacy_mode is None
    assert batch.valid_for_ns is None


@pytest.mark.parametrize(
    'changes',
    [
        {'frame_generation': 2},
        {'last_valid_frame_boottime_ns': 0},
        {'last_valid_frame_boottime_ns': _NOW_NS + 1},
        {
            'camera_available_state': HOMECAM_STATE_FALSE,
            'last_valid_frame_boottime_ns': _NOW_NS - 1,
            'frame_generation': 1,
        },
        {
            'privacy_mode_state': HOMECAM_STATE_TRUE,
        },
        {
            'active_session_id': _MEDIA_SESSION,
            'active_session_generation': 2,
        },
        {
            'active_session_id': '',
            'active_session_generation': 1,
        },
        {
            'backend_device_bound': False,
        },
        {
            'physical_authority': False,
        },
    ],
)
def test_homecam_authority_frame_and_session_relations_fail_closed(
    changes,
) -> None:
    """Inconsistent related fields clear camera and privacy as one unit."""
    store = _store(_Clock())
    batch = _media_tracker().observe(
        _media_message(**changes),
        binding_token=store.binding_token(),
        received_boottime_ns=_NOW_NS,
    )
    assert batch is not None
    assert (batch.camera_available, batch.privacy_mode) == (None, None)


def test_homecam_valid_false_true_and_unknown_states_remain_distinct() -> None:
    """Explicit unavailability/privacy and unknown are not conflated."""
    store = _store(_Clock())
    tracker = _media_tracker()
    private = tracker.observe(
        _media_message(
            camera_available_state=HOMECAM_STATE_FALSE,
            privacy_mode_state=HOMECAM_STATE_TRUE,
            last_valid_frame_boottime_ns=0,
            frame_generation=0,
        ),
        binding_token=store.binding_token(),
        received_boottime_ns=_NOW_NS,
    )
    assert private is not None
    assert (private.camera_available, private.privacy_mode) == (False, True)
    unknown = tracker.observe(
        _media_message(
            sequence=2,
            camera_available_state=HOMECAM_STATE_UNKNOWN,
            privacy_mode_state=HOMECAM_STATE_UNKNOWN,
            last_valid_frame_boottime_ns=0,
            frame_generation=0,
            active_session_id='',
            active_session_generation=0,
        ),
        binding_token=store.binding_token(),
        received_boottime_ns=_NOW_NS + 1,
    )
    assert unknown is not None
    assert (unknown.camera_available, unknown.privacy_mode) == (None, None)


def test_homecam_nonphysical_observer_accepts_bound_nonauthoritative_media(
) -> None:
    """Simulation may report media state but cannot gain physical authority."""
    store = _store(_Clock(), authority=False)
    batch = _media_tracker(authority=False).observe(
        _media_message(physical_authority=False),
        binding_token=store.binding_token(),
        received_boottime_ns=_NOW_NS,
    )
    assert batch is not None
    assert (batch.camera_available, batch.privacy_mode) == (True, False)


def test_homecam_current_generation_session_is_valid_metadata_only() -> None:
    """A UUIDv4 session may accompany privacy-off without upgrading state."""
    store = _store(_Clock())
    batch = _media_tracker().observe(
        _media_message(
            active_session_id=_MEDIA_SESSION,
            active_session_generation=1,
        ),
        binding_token=store.binding_token(),
        received_boottime_ns=_NOW_NS,
    )
    assert batch is not None
    assert (batch.camera_available, batch.privacy_mode) == (True, False)


def test_homecam_exact_duplicate_is_noop_but_conflict_clears() -> None:
    """DDS duplicates do not refresh TTL; same-sequence changes do clear."""
    store = _store(_Clock())
    tracker = _media_tracker()
    message = _media_message()
    accepted = tracker.observe(
        message,
        binding_token=store.binding_token(),
        received_boottime_ns=_NOW_NS,
    )
    assert accepted is not None
    duplicate = tracker.observe(
        message,
        binding_token=store.binding_token(),
        received_boottime_ns=_NOW_NS + 1,
    )
    assert duplicate is None
    conflict = tracker.observe(
        _media_message(
            camera_available_state=HOMECAM_STATE_FALSE,
            last_valid_frame_boottime_ns=0,
            frame_generation=0,
        ),
        binding_token=store.binding_token(),
        received_boottime_ns=_NOW_NS + 2,
    )
    assert conflict is not None
    assert (conflict.camera_available, conflict.privacy_mode) == (None, None)


def test_homecam_sequence_and_generation_regressions_clear() -> None:
    """Require advancing sequence without generation rollback."""
    store = _store(_Clock())
    tracker = _media_tracker()
    assert tracker.observe(
        _media_message(sequence=2, control_plane_generation=2,
                       frame_generation=2),
        binding_token=store.binding_token(),
        received_boottime_ns=_NOW_NS,
    ).camera_available is True
    for message in (
        _media_message(sequence=1),
        _media_message(sequence=3),
    ):
        cleared = tracker.observe(
            message,
            binding_token=store.binding_token(),
            received_boottime_ns=_NOW_NS + 1,
        )
        assert cleared is not None
        assert (cleared.camera_available, cleared.privacy_mode) == (None, None)


def test_homecam_restart_uses_two_phase_handoff_and_retires_old_source(
) -> None:
    """A new process needs two fresh messages before replacing the old one."""
    store = _store(_Clock())
    tracker = _media_tracker()
    token = store.binding_token()
    first = tracker.observe(
        _media_message(),
        binding_token=token,
        received_boottime_ns=_NOW_NS,
    )
    assert first.camera_available is True
    candidate = tracker.observe(
        _media_message(source_instance_id=_MEDIA_SOURCE_B),
        binding_token=token,
        received_boottime_ns=_NOW_NS + 1,
    )
    assert candidate is not None
    assert (candidate.camera_available, candidate.privacy_mode) == (None, None)
    accepted = tracker.observe(
        _media_message(
            source_instance_id=_MEDIA_SOURCE_B,
            sequence=2,
        ),
        binding_token=token,
        received_boottime_ns=_NOW_NS + 2,
    )
    assert accepted is not None
    assert accepted.camera_available is True
    old_source = tracker.observe(
        _media_message(source_instance_id=_MEDIA_SOURCE_A, sequence=2),
        binding_token=token,
        received_boottime_ns=_NOW_NS + 3,
    )
    assert old_source is not None
    assert (
        old_source.camera_available,
        old_source.privacy_mode,
    ) == (None, None)


def test_homecam_candidate_requires_higher_consistent_message() -> None:
    """A duplicate or rollback cannot complete a producer handoff."""
    store = _store(_Clock())
    tracker = _media_tracker()
    token = store.binding_token()
    tracker.observe(
        _media_message(),
        binding_token=token,
        received_boottime_ns=_NOW_NS,
    )
    candidate_message = _media_message(
        source_instance_id=_MEDIA_SOURCE_B,
        sequence=4,
        control_plane_generation=2,
        frame_generation=2,
    )
    tracker.observe(
        candidate_message,
        binding_token=token,
        received_boottime_ns=_NOW_NS + 1,
    )
    assert tracker.observe(
        candidate_message,
        binding_token=token,
        received_boottime_ns=_NOW_NS + 2,
    ) is None
    rollback = tracker.observe(
        _media_message(source_instance_id=_MEDIA_SOURCE_B, sequence=5),
        binding_token=token,
        received_boottime_ns=_NOW_NS + 3,
    )
    assert rollback is not None
    assert rollback.camera_available is None
    accepted = tracker.observe(
        _media_message(
            source_instance_id=_MEDIA_SOURCE_B,
            sequence=6,
            control_plane_generation=2,
            frame_generation=2,
        ),
        binding_token=token,
        received_boottime_ns=_NOW_NS + 4,
    )
    assert accepted is not None
    assert accepted.camera_available is True


def test_homecam_expiry_is_one_shot_at_exact_local_boundary() -> None:
    """Media state clears exactly once when its bounded local TTL ends."""
    store = _store(_Clock())
    tracker = _media_tracker(lifetime=0.25)
    tracker.observe(
        _media_message(),
        binding_token=store.binding_token(),
        received_boottime_ns=_NOW_NS,
    )
    assert tracker.expire(
        binding_token=store.binding_token(),
        now_boottime_ns=_NOW_NS + 249_999_999,
    ) is None
    expired = tracker.expire(
        binding_token=store.binding_token(),
        now_boottime_ns=_NOW_NS + 250_000_000,
    )
    assert expired is not None
    assert (expired.camera_available, expired.privacy_mode) == (None, None)
    assert tracker.expire(
        binding_token=store.binding_token(),
        now_boottime_ns=_NOW_NS + 250_000_001,
    ) is None


def test_homecam_local_boottime_regression_clears() -> None:
    """A callback clock rollback cannot create or refresh trusted state."""
    store = _store(_Clock())
    tracker = _media_tracker()
    accepted = tracker.observe(
        _media_message(),
        binding_token=store.binding_token(),
        received_boottime_ns=_NOW_NS,
    )
    assert accepted is not None
    regressed = tracker.observe(
        _media_message(sequence=2),
        binding_token=store.binding_token(),
        received_boottime_ns=_NOW_NS - 1,
    )
    assert regressed is not None
    assert (regressed.camera_available, regressed.privacy_mode) == (None, None)
    with pytest.raises(ValueError, match='regressed'):
        tracker.expire(
            binding_token=store.binding_token(),
            now_boottime_ns=_NOW_NS - 1,
        )


def test_homecam_atomic_clear_and_map_binding_cas() -> None:
    """Clear both fields once; let stale map tokens commit neither."""
    clock = _Clock()
    store = _store(clock)
    publisher = HomecamMediaObservationPublisher(store)
    tracker = _media_tracker()
    token = store.binding_token()
    known = tracker.observe(
        _media_message(),
        binding_token=token,
        received_boottime_ns=_NOW_NS,
    )
    publisher.publish(known)
    clock.value += 1
    clear = tracker.observe(
        _media_message(
            sequence=2,
            camera_available_state=HOMECAM_STATE_UNKNOWN,
            privacy_mode_state=HOMECAM_STATE_UNKNOWN,
            last_valid_frame_boottime_ns=0,
            frame_generation=0,
        ),
        binding_token=token,
        received_boottime_ns=clock.value,
    )
    assert publisher.publish(clear) == 2
    evidence = _parse(store, clock)
    assert evidence.camera_available is None
    assert evidence.privacy_mode is None

    race_clock = _Clock()
    race_store = _store(race_clock)

    def advancing_clock():
        race_clock.value += 1
        return race_clock.value

    race_publisher = HomecamMediaObservationPublisher._for_test(
        race_store,
        boottime_ns=advancing_clock,
    )
    race_tracker = _media_tracker()
    stale_token = race_store.binding_token()
    race_known = race_tracker.observe(
        _media_message(),
        binding_token=stale_token,
        received_boottime_ns=_NOW_NS,
    )
    race_publisher.publish(race_known)
    race_clock.value += 1
    rejected = race_tracker.observe(
        _media_message(sequence=2, device_id='another-device'),
        binding_token=stale_token,
        received_boottime_ns=race_clock.value,
    )
    race_store.update_binding('second-map', 'grid-8')
    race_clock.value += 1
    race_publisher.publish_fail_closed(rejected)
    rebound = _parse(race_store, race_clock)
    assert rebound.camera_available is None
    assert rebound.privacy_mode is None


def test_homecam_same_receipt_invalid_message_still_clears() -> None:
    """A non-strict clock tick cannot leave prior media state known."""
    clock = _Clock()
    store = _store(clock)

    def next_tick():
        clock.value += 1
        return clock.value

    publisher = HomecamMediaObservationPublisher._for_test(
        store,
        boottime_ns=next_tick,
    )
    tracker = _media_tracker()
    token = store.binding_token()
    known = tracker.observe(
        _media_message(),
        binding_token=token,
        received_boottime_ns=_NOW_NS,
    )
    publisher.publish(known)
    invalid = tracker.observe(
        _media_message(sequence=2, device_id='another-device'),
        binding_token=token,
        received_boottime_ns=_NOW_NS,
    )
    publisher.publish_fail_closed(invalid)
    evidence = _parse(store, clock)
    assert evidence.camera_available is None
    assert evidence.privacy_mode is None


def test_homecam_topic_and_qos_are_fixed_reliable_volatile_depth_one() -> None:
    """The trusted media boundary uses one non-configurable local ROS topic."""
    qos = observer_module._HOMECAM_MEDIA_QOS
    assert HOMECAM_MEDIA_EVIDENCE_TOPIC == '/homecam/media_evidence'
    assert qos.depth == 1
    assert qos.reliability == observer_module.ReliabilityPolicy.RELIABLE
    assert qos.durability == observer_module.DurabilityPolicy.VOLATILE
    assert qos.history == observer_module.HistoryPolicy.KEEP_LAST


def test_observer_source_contains_no_physical_command_calls() -> None:
    """The trusted observer may inspect readiness but never issue commands."""
    source = inspect.getsource(observer_module)
    tree = ast.parse(source)
    forbidden_calls = {
        'send_goal',
        'send_goal_async',
        'cancel_goal',
        'cancel_goal_async',
        'create_publisher',
    }
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
    }
    assert not called.intersection(forbidden_calls)
    assert '/cmd_vel' not in source
    assert '.server_is_ready()' in source
    assert '.service_is_ready()' in source


def test_live_node_serves_incomplete_evidence_and_closes_socket(
    tmp_path,
) -> None:
    """The real UDS lifecycle works without fabricating missing safety data."""
    if not rclpy_ok_for_test():
        pytest.skip('rclpy context is not available')
    import rclpy
    from rclpy.parameter import Parameter
    from malbut_interfaces.msg import HomecamMediaEvidence

    socket_path = tmp_path / 'robot-state.sock'
    overrides = [
        Parameter('device_id', value='malbut-device-01'),
        Parameter('map_id', value='home-map'),
        Parameter('map_revision', value='grid-7'),
        Parameter('socket_path', value=str(socket_path)),
        Parameter('expected_agent_uid', value=os.geteuid()),
        Parameter('physical_authority', value=True),
        Parameter('use_sim_time', value=False),
    ]
    rclpy.init()
    node = None
    media_node = None
    try:
        node = observer_module.TrustedRobotStateObserver(
            parameter_overrides=overrides,
        )
        changed = node.set_parameters([
            Parameter('use_sim_time', value=True),
        ])
        assert len(changed) == 1
        assert changed[0].successful is False
        assert node.get_parameter('use_sim_time').value is False
        source = UnixSocketTrustedRobotStateSource(
            str(socket_path),
            os.geteuid(),
            'malbut-device-01',
            timeout_seconds=1.0,
        )
        evidence = source.read()
        assert evidence.navigation_available is None
        assert evidence.localization_ok is None
        assert evidence.battery_percent is None
        media_node = rclpy.create_node('homecam_media_evidence_test')
        media_publisher = media_node.create_publisher(
            HomecamMediaEvidence,
            HOMECAM_MEDIA_EVIDENCE_TOPIC,
            observer_module._HOMECAM_MEDIA_QOS,
        )
        discovery_deadline = time.monotonic() + 2.0
        while (
            media_publisher.get_subscription_count() < 1
            and time.monotonic() < discovery_deadline
        ):
            rclpy.spin_once(node, timeout_sec=0.02)
        assert media_publisher.get_subscription_count() == 1
        receipt = trusted_boottime_ns()
        message = HomecamMediaEvidence()
        message.schema_version = 1
        message.device_id = 'malbut-device-01'
        message.source_instance_id = _MEDIA_SOURCE_A
        message.sequence = 1
        message.control_plane_generation = 1
        message.observed_boottime_ns = receipt
        message.valid_until_boottime_ns = receipt + 500_000_000
        message.camera_available_state = HOMECAM_STATE_TRUE
        message.privacy_mode_state = HOMECAM_STATE_FALSE
        message.last_valid_frame_boottime_ns = receipt
        message.frame_generation = 1
        message.active_session_id = ''
        message.active_session_generation = 0
        message.backend_device_bound = True
        message.physical_authority = True
        media_publisher.publish(message)
        delivery_deadline = time.monotonic() + 1.0
        media_evidence = None
        while time.monotonic() < delivery_deadline:
            rclpy.spin_once(node, timeout_sec=0.02)
            media_evidence = source.read()
            if media_evidence.camera_available is True:
                break
        assert media_evidence.camera_available is True
        assert media_evidence.privacy_mode is False
    finally:
        if media_node is not None:
            media_node.destroy_node()
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    assert not socket_path.exists()


def rclpy_ok_for_test() -> bool:
    """Keep the live lifecycle test explicit and easy to skip in isolation."""
    try:
        import rclpy  # noqa: F401
    except ImportError:
        return False
    return True
