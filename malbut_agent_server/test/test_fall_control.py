"""Settings/heartbeat/timeouts against the real core and a fake Cloud provider."""

import asyncio

import pytest

from malbut_agent_server.application.fall_detector_input import FallDetectorInput
from malbut_agent_server.fall_control import FallSettingsControl
from malbut_agent_server.ports.cloud_fall import CloudFallProviderError
from test_cloud_fall_monitor import make, frame, candidate


def setup_control(**policy):
    monitor, clock, provider = make(**policy)
    control = FallSettingsControl(FallDetectorInput(monitor, max_source_age_s=2),
                                  manager_runtime_id='manager-1', clock=clock)
    return control, monitor, clock, provider


def settings(control, **changes):
    return dict(runtime_id=control.runtime_id, settings_revision=1, enabled=True,
                camera_enabled=True, cloud_consent=True) | changes


def heartbeat(control, clock, sequence=1, **changes):
    return dict(manager_runtime_id='manager-1', runtime_id=control.runtime_id,
                sequence=sequence, settings_revision=1,
                server_checked_at=clock(), sent_at=clock()) | changes


def start(control, clock):
    assert control.apply_settings(**settings(control))['applied']
    assert control.heartbeat(**heartbeat(control, clock))
    assert control.accepting_images and control.cloud_block_reason is None


def test_starts_off_requires_bound_manager_settings_and_fresh_heartbeat():
    ctl, monitor, clock, provider = setup_control()
    assert ctl.status()['runtime_state'] == 'waiting_settings'
    assert not monitor.ingest_rgb(frame(clock()))
    assert not asyncio.run(monitor.run_once())
    ctl.apply_settings(**settings(ctl))
    assert not ctl.accepting_images
    ctl.heartbeat(**heartbeat(ctl, clock, server_checked_at=-1.0))
    assert not ctl.accepting_images
    ctl.heartbeat(**heartbeat(ctl, clock, 2))
    assert ctl.accepting_images and not provider.calls
    unbound = FallSettingsControl(ctl.adapter, clock=clock)
    assert unbound.apply_settings(**settings(unbound))['reason_code'] == 'invalid_request'
    assert not unbound.heartbeat(**heartbeat(unbound, clock))
    assert not unbound.accepting_images


@pytest.mark.parametrize('changes,reason', [
    ({'runtime_id': 'old-run'}, 'runtime_mismatch'),
    ({'settings_revision': 0}, 'invalid_request'),
    ({'settings_revision': True}, 'invalid_request'),
    ({'settings_revision': 2**64}, 'invalid_request'),
    ({'enabled': 1}, 'invalid_request'),
    ({'camera_enabled': None}, 'invalid_request'),
    ({'cloud_consent': 'true'}, 'invalid_request'),
    ({'extra': True}, 'invalid_request'),
    ({'settings_revision': 1}, 'stale_revision'),
    ({'settings_revision': 2, 'enabled': False}, 'revision_conflict'),
])
def test_invalid_settings_do_not_replace_applied_settings(changes, reason):
    ctl, _, clock, _ = setup_control()
    start(ctl, clock)
    ctl.apply_settings(**settings(ctl, settings_revision=2))
    result = ctl.apply_settings(**settings(ctl, **changes))
    assert not result['applied'] and result['reason_code'] == reason
    assert result['applied_revision'] == 2 and result['enabled']


def test_duplicate_settings_do_not_extend_five_second_connection():
    ctl, monitor, clock, _ = setup_control()
    start(ctl, clock)
    monitor.ingest_rgb(frame(clock()))
    iid = monitor.candidate(candidate())
    clock.value = 104.999
    assert ctl.apply_settings(**settings(ctl))['reason_code'] == 'already_applied'
    assert ctl.accepting_images
    clock.value = 105
    status = ctl.status()
    assert not status['accepting_images'] and status['pause_reason'] == 'control_unavailable'
    assert status['settings_applied'] and status['enabled'] and status['applied_revision'] == 1
    assert monitor.buffer.stored_bytes == 0
    assert not monitor.incident(iid).pending


@pytest.mark.parametrize('changes', [
    {'manager_runtime_id': 'old-manager'}, {'runtime_id': 'old-vlm'},
    {'sequence': 1}, {'sequence': 0}, {'sequence': True}, {'sequence': 2**64},
    {'sent_at': 106.0}, {'sent_at': 99.0}, {'sent_at': float('nan')},
    {'server_checked_at': 106.0}, {'server_checked_at': float('inf')},
    {'server_checked_at': -2}, {'server_checked_at': True},
    {'settings_revision': -1}, {'extra': 'field'},
])
def test_invalid_heartbeats_cannot_keep_connection_alive(changes):
    ctl, _, clock, _ = setup_control()
    start(ctl, clock)
    clock.value = 104
    assert not ctl.heartbeat(**(heartbeat(ctl, clock, 2) | changes))
    clock.value = 105
    assert not ctl.accepting_images


def test_server_fifteen_second_timeout_keeps_local_input_but_not_cloud_queue():
    ctl, monitor, clock, provider = setup_control()
    start(ctl, clock)
    for seq in range(2, 17):
        clock.value += 1
        ctl.heartbeat(**heartbeat(ctl, clock, seq, server_checked_at=100.0))
    assert clock() == 115 and ctl.accepting_images
    assert ctl.cloud_block_reason == 'server_settings_stale'
    assert monitor.ingest_rgb(frame(clock()))
    iid = monitor.candidate(candidate(clock()))
    assert not monitor.incident(iid).pending
    assert monitor.incident(iid).attempts == 0
    assert not monitor.request_recheck(iid)
    assert not asyncio.run(monitor.run_once())
    assert monitor.analysis_status.state == 'idle'
    assert monitor.analysis_status.request_id == '' and not provider.calls
    ctl.heartbeat(**heartbeat(ctl, clock, 17))
    assert ctl.cloud_block_reason is None
    assert not asyncio.run(monitor.run_once())  # Do not upload the offline backlog.
    assert monitor.request_recheck(iid)  # Explicit fresh recheck remains possible.
    assert asyncio.run(monitor.run_once())
    assert len(provider.calls) == 1


@pytest.mark.parametrize('first', ['settings', 'heartbeat'])
def test_internal_recovery_requires_both_settings_check_and_new_server_proof(first):
    ctl, _, clock, _ = setup_control()
    start(ctl, clock)
    clock.value = 105
    assert not ctl.accepting_images
    assert ctl.heartbeat(**heartbeat(ctl, clock, 2, server_checked_at=100.0))
    assert not ctl.accepting_images
    if first == 'settings':
        ctl.apply_settings(**settings(ctl))
        assert not ctl.accepting_images
        ctl.heartbeat(**heartbeat(ctl, clock, 3))
    else:
        ctl.heartbeat(**heartbeat(ctl, clock, 3))
        assert not ctl.accepting_images
        ctl.apply_settings(**settings(ctl))
    assert ctl.accepting_images and ctl.cloud_block_reason is None


def test_rejected_server_proof_cannot_be_restored_from_cached_confirmation():
    ctl, _, clock, _ = setup_control()
    start(ctl, clock)
    ctl.heartbeat(**heartbeat(ctl, clock, 2, server_checked_at=-1.0))
    assert ctl.accepting_images
    assert ctl.cloud_block_reason == 'server_settings_unavailable'
    ctl.heartbeat(**heartbeat(ctl, clock, 3, server_checked_at=100.0))
    assert ctl.cloud_block_reason == 'server_settings_unavailable'
    clock.value += 0.1
    ctl.heartbeat(**heartbeat(ctl, clock, 4))
    assert ctl.cloud_block_reason is None


def test_revision_mismatch_blocks_cloud_and_off_settings_apply_immediately():
    ctl, monitor, clock, _ = setup_control()
    start(ctl, clock)
    ctl.heartbeat(**heartbeat(ctl, clock, 2, settings_revision=2))
    assert ctl.accepting_images and ctl.cloud_block_reason == 'settings_pending'
    monitor.ingest_rgb(frame(clock()))
    ctl.apply_settings(**settings(ctl, settings_revision=2, camera_enabled=False))
    assert not ctl.accepting_images and monitor.buffer.stored_bytes == 0
    assert ctl.status()['pause_reason'] == 'camera_off'


@pytest.mark.parametrize('cause', ['manager', 'server', 'consent'])
def test_inflight_cancellation_keeps_case_open_and_status_matches_real_task(cause):
    async def run():
        ctl, monitor, clock, provider = setup_control()
        start(ctl, clock)
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        provider.release = asyncio.Event()
        task = asyncio.create_task(monitor.run_once())
        await provider.started.wait()
        status = monitor.analysis_status
        assert status.state == 'waiting_response'
        assert status.request_id == provider.calls[0].request_id
        assert status.request_purpose == 'incident'
        if cause == 'manager':
            clock.value = 105
            ctl.refresh()
        elif cause == 'server':
            ctl.heartbeat(**heartbeat(ctl, clock, 2, server_checked_at=-1.0))
        else:
            ctl.apply_settings(**settings(ctl, settings_revision=2, cloud_consent=False))
        assert monitor.analysis_status.state == 'cancel_requested'
        await task
        assert monitor.analysis_status.state == 'canceled'
        assert monitor.analysis_status.last_error_code == ''
        assert monitor.incident(iid).video is None
        assert not monitor.incident(iid).pending
        assert ctl.accepting_images == (cause != 'manager')
    asyncio.run(run())


def test_provider_ignoring_cancel_cannot_apply_late_normal_result():
    async def run():
        ctl, monitor, clock, provider = setup_control(cloud_timeout_s=0.02)
        release = asyncio.Event()
        original = provider.analyze

        async def ignore_cancel(request):
            reply = await original(request)
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return reply

        provider.analyze = ignore_cancel
        start(ctl, clock)
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        task = asyncio.create_task(monitor.run_once())
        await provider.started.wait()
        ctl.apply_settings(**settings(ctl, settings_revision=2, cloud_consent=False))
        await task
        assert monitor.analysis_status.state == 'cancel_requested'
        assert monitor.incident(iid).video is None
        release.set()
        await asyncio.sleep(0)
        assert monitor.analysis_status.state == 'canceled'
        assert monitor.incident(iid).video is None
    asyncio.run(run())


@pytest.mark.parametrize('error,expected', [
    (None, 'completed'), (CloudFallProviderError('cloud_auth_required'), 'failed'),
    (ValueError('private provider contents'), 'failed'),
])
def test_analysis_status_records_actual_success_or_safe_failure(error, expected):
    ctl, monitor, clock, provider = setup_control()
    start(ctl, clock)
    provider.error = error
    monitor.ingest_rgb(frame(clock()))
    monitor.candidate(candidate())
    assert asyncio.run(monitor.run_once())
    status = monitor.analysis_status
    assert status.state == expected and status.request_id == provider.calls[0].request_id
    assert status.last_error_code == ('' if error is None else 'cloud_auth_required'
                                      if isinstance(error, CloudFallProviderError)
                                      else 'cloud_failed_or_invalid_response')


def test_actual_cloud_timeout_is_failed_not_a_health_check():
    async def run():
        ctl, monitor, clock, provider = setup_control(cloud_timeout_s=0.01)
        start(ctl, clock)
        monitor.ingest_rgb(frame(clock()))
        monitor.candidate(candidate())
        provider.release = asyncio.Event()
        assert await monitor.run_once()
        assert monitor.analysis_status.state == 'failed'
        assert monitor.analysis_status.last_error_code == 'cloud_timeout'
    asyncio.run(run())


def test_adapter_failure_does_not_report_new_settings_as_applied(monkeypatch):
    ctl, _, clock, _ = setup_control()
    ctl.heartbeat(**heartbeat(ctl, clock))

    def failed(**kwargs):
        raise RuntimeError('private details')

    monkeypatch.setattr(ctl.adapter, 'configure', failed)
    result = ctl.apply_settings(**settings(ctl))
    assert result['reason_code'] == 'internal_error' and not result['applied']
    assert result['applied_revision'] == 0
    assert ctl.status()['runtime_state'] == 'error'
    assert not ctl.accepting_images


def test_backwards_clock_stops_control():
    ctl, _, clock, _ = setup_control()
    start(ctl, clock)
    clock.value = 99
    assert ctl.status()['runtime_state'] == 'error'
    assert not ctl.accepting_images
