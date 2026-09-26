"""Settings relay contracts with fake time; no ROS graph or provider calls."""

from unittest.mock import Mock

import pytest

from malbut_fall_coordinator.fall_settings import FallSettingsRelay


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


def make():
    clock = Clock()
    relay = FallSettingsRelay(manager_id='manager', bridge_id='bridge', vlm_id='vlm', clock=clock)
    return relay, clock


def snapshot(**changes):
    return dict(bridge_runtime_id='bridge', sequence=2, observed_at=100.0,
                check_state='confirmed', settings_revision=1, enabled=True,
                camera_enabled=True, cloud_consent=True,
                server_checked_at=100.0, reason_code='none') | changes


def status(**changes):
    return dict(runtime_id='vlm', sequence=1, applied_revision=0, settings_applied=False,
                enabled=False, camera_enabled=False, cloud_consent=False,
                runtime_state='waiting_settings', pause_reason='waiting_settings') | changes


def reply(**changes):
    return dict(runtime_id='vlm', requested_revision=1, applied_revision=1, applied=True,
                enabled=True, camera_enabled=True, cloud_consent=True,
                reason_code='applied') | changes


def prepared():
    relay, clock = make()
    assert relay.status(status())
    assert relay.snapshot(snapshot())
    return relay, clock


def test_no_settings_no_call_and_no_fabricated_confirmation():
    relay, _ = make()
    assert relay.heartbeat() is None and relay.request() is None
    relay.status(status())
    assert relay.request() is None
    assert relay.heartbeat()['server_checked_at'] == -1
    assert relay.heartbeat()['settings_revision'] == 0


def test_apply_once_original_snapshot_report_and_original_server_time():
    relay, clock = prepared()
    call_id, request = relay.request()
    assert request == dict(runtime_id='vlm', settings_revision=1, enabled=True,
                           camera_enabled=True, cloud_consent=True)
    clock.value = 101
    relay.snapshot(snapshot(sequence=3, observed_at=101., server_checked_at=101.))
    report = relay.complete(call_id, reply())
    assert report['snapshot_sequence'] == 2 and report['reported_at'] == 101
    assert report['manager_runtime_id'] == 'manager' and report['bridge_runtime_id'] == 'bridge'
    assert relay.request() is None
    clock.value = 102
    relay.snapshot(snapshot(sequence=4, observed_at=102., server_checked_at=102.))
    assert relay.request() is None
    clock.value = 103
    assert relay.heartbeat()['server_checked_at'] == 102
    assert relay.complete(call_id, reply()) is None


@pytest.mark.parametrize('change', [
    {'bridge_runtime_id': 'old'}, {'sequence': 0}, {'sequence': True},
    {'settings_revision': 0}, {'settings_revision': True}, {'settings_revision': 2**64},
    {'enabled': 1}, {'camera_enabled': 'true'}, {'cloud_consent': None},
    {'observed_at': 101}, {'observed_at': float('nan')}, {'server_checked_at': 101},
    {'server_checked_at': 99}, {'server_checked_at': True}, {'reason_code': 'whatever'},
    {'check_state': []},
])
def test_bad_snapshot_cannot_enable(change):
    relay, _ = make()
    relay.status(status())
    assert not relay.snapshot(snapshot(**change))
    assert relay.request() is None


def test_old_duplicate_or_wrong_peer_does_not_revoke_new_settings():
    relay, _ = prepared()
    assert not relay.snapshot(snapshot(bridge_runtime_id='old', sequence=20))
    assert not relay.snapshot(snapshot(sequence=2, enabled=False))
    assert relay.request() is not None


def test_same_revision_conflict_invalidates_and_requires_fresh_server_proof():
    relay, clock = prepared()
    assert not relay.snapshot(snapshot(sequence=3, enabled=False))
    assert relay.heartbeat()['server_checked_at'] == -1 and relay.request() is None
    assert not relay.snapshot(snapshot(sequence=4))
    clock.value = 101
    assert relay.snapshot(snapshot(sequence=5, observed_at=101., server_checked_at=101.))
    assert relay.request() is not None


def test_transport_failure_preserves_proof_but_invalid_server_reply_revokes_it():
    relay, clock = prepared()
    clock.value = 102
    assert relay.snapshot(snapshot(sequence=3, observed_at=102., check_state='unavailable',
                                   reason_code='server_timeout'))
    assert relay.heartbeat()['server_checked_at'] == 100
    assert relay.snapshot(snapshot(sequence=4, observed_at=102., check_state='rejected',
                                   reason_code='server_settings_missing', server_checked_at=-1.))
    assert relay.heartbeat()['server_checked_at'] == -1
    assert relay.request() is None


def test_expired_server_proof_never_starts_service_despite_live_vlm():
    relay, clock = prepared()
    for second in range(1, 16):
        clock.value = 100 + second
        relay.status(status(sequence=second + 1))
    assert relay.request() is None
    assert relay.heartbeat()['server_checked_at'] == 100


@pytest.mark.parametrize('change', [
    {'runtime_id': 'old'}, {'requested_revision': 2}, {'requested_revision': True},
    {'applied_revision': 0}, {'applied': False}, {'enabled': False},
    {'reason_code': 'internal_error'}, {'extra': 'not-native'},
])
def test_mismatched_reply_does_not_become_a_report(change):
    relay, _ = prepared()
    call_id, _ = relay.request()
    assert relay.complete(call_id, reply(**change)) is None
    assert relay.report_sequence == 0


def test_failed_service_reply_is_not_reported_as_applied():
    relay, _ = prepared()
    call_id, _ = relay.request()
    report = relay.complete(call_id, reply(applied=False, reason_code='internal_error',
                                           applied_revision=0, enabled=False,
                                           camera_enabled=False, cloud_consent=False))
    assert not report['applied'] and report['applied_revision'] == 0


def test_async_timeout_heartbeat_continues_and_late_reply_cannot_report_success():
    relay, clock = prepared()
    call_id, _ = relay.request()
    for tick in (101, 102, 103):
        clock.value = tick
        assert relay.heartbeat() is not None
    assert relay.complete(call_id, reply()) is None
    assert relay.request() is None
    clock.value = 104
    new_call_id, _ = relay.request()
    assert new_call_id != call_id
    assert relay.complete(call_id, reply()) is None
    assert relay.complete(new_call_id, reply(reason_code='already_applied'))['applied']


def test_status_timeout_needs_new_proof_and_service_again():
    relay, clock = prepared()
    call_id, _ = relay.request()
    relay.complete(call_id, reply())
    clock.value = 105
    assert relay.heartbeat() is None
    relay.status(status(sequence=2))
    assert relay.request() is None
    assert relay.heartbeat()['server_checked_at'] == -1
    relay.snapshot(snapshot(sequence=3, observed_at=105., server_checked_at=105.))
    assert relay.request() is not None


def test_changed_setting_dispatches_after_old_reply_without_losing_revision():
    relay, clock = prepared()
    old_id, _ = relay.request()
    clock.value = 101
    relay.snapshot(snapshot(sequence=3, observed_at=101., server_checked_at=101.,
                            settings_revision=2, enabled=False))
    assert relay.complete(old_id, reply())['requested_revision'] == 1
    clock.value = 102
    new_id, request = relay.request()
    assert request['settings_revision'] == 2 and not request['enabled']
    assert new_id != old_id


@pytest.mark.parametrize('gap', ['vlm_status', 'manager_heartbeat'])
def test_real_vlm_control_applies_and_recovers_without_provider_calls(gap):
    module = pytest.importorskip('malbut_agent_server.fall_control')
    FallSettingsControl = module.FallSettingsControl
    relay, clock = make()
    adapter = Mock()
    control = FallSettingsControl(adapter, manager_runtime_id='manager', runtime_id='vlm',
                                  clock=clock)

    def send_status():
        return relay.status(dict(control.status(), sequence=relay.status_sequence + 1))

    send_status()
    relay.snapshot(snapshot())
    call_id, request = relay.request()
    assert relay.complete(call_id, control.apply_settings(**request))['applied']
    control.heartbeat(**relay.heartbeat())
    assert control.accepting_images and control.cloud_block_reason is None
    send_status()
    # Simulate only one direction failing; provider is never called for heartbeat.
    for tick in range(101, 106):
        clock.value = tick
        if gap == 'manager_heartbeat':
            send_status()
    assert not control.accepting_images
    send_status()
    control.heartbeat(**relay.heartbeat())
    assert relay.request() is None
    clock.value = 106
    relay.snapshot(snapshot(sequence=3, observed_at=106., server_checked_at=106.))
    call_id, request = relay.request()
    result = control.apply_settings(**request)
    assert result['reason_code'] == 'already_applied'
    relay.complete(call_id, result)
    control.heartbeat(**relay.heartbeat())
    assert control.accepting_images and control.cloud_block_reason is None
    assert not adapter.analyze.called


def test_shutdown_or_bad_clock_stops_heartbeat():
    relay, clock = prepared()
    clock.value = 99
    assert relay.heartbeat() is None
    relay, _ = prepared()
    relay.close()
    assert relay.request() is None and relay.heartbeat() is None
