"""Tests for the injected Gazebo monitor-room Nav2 controller."""

from concurrent.futures import ThreadPoolExecutor
import ast
from dataclasses import replace
import inspect
from threading import Event

import pytest

import malbut_gazebo.gazebo_monitor_room_nav2_adapter as adapter_module
from malbut_gazebo.gazebo_monitor_room_nav2_adapter import (
    GazeboMonitorRoomNav2AdapterError,
    GazeboMonitorRoomNav2Controller,
    Nav2CancelRequest,
    Nav2StartRequest,
    _preflight_request_fingerprint,
)
from malbut_gazebo.gazebo_monitor_room_store import (
    CancelOperation,
    GazeboMonitorRoomClockRollbackError,
    GazeboMonitorRoomStore,
    OrderedSemanticSample,
    PrepareOperation,
)


_DIGEST = 'a' * 64
_OTHER_DIGEST = 'b' * 64


class _MaliciousStatus(str):
    def __format__(self, _format_spec):
        return 'private_secret_marker'


def _request(*, deadline=100.0, samples=None):
    return PrepareOperation(
        prepare_request_id='prepare-1',
        operation_id='operation-1',
        robot_id='robot-1',
        map_id='home-map',
        map_revision='map-revision-1',
        semantic_revision='semantic-revision-1',
        zones_digest=_DIGEST,
        target_binding_digest=_DIGEST,
        effects_digest=_DIGEST,
        profile_digest=_DIGEST,
        plan_digest=_DIGEST,
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


class _Clock:
    def __init__(self, value=2.0):
        self.value = value

    def __call__(self):
        return self.value


class _Port:
    def __init__(self):
        self.preflights = []
        self.sends = []
        self.observes = []
        self.cancels = []
        self.preflight_hook = None
        self.send_hook = None
        self.observe_hook = None
        self.cancel_hook = None
        self.send_error = None
        self.expected_start_fence = None
        self.preflight_reports = []
        self.send_reports = []
        self.observe_reports = []
        self.cancel_reports = []

    def preflight(self, request):
        self.preflights.append(request)
        if self.preflight_hook is not None:
            self.preflight_hook(request)
        report = {
            'operation_id': request.operation_id,
            'goal_uuid': request.goal_uuid,
            'binding_digest': request.binding_digest,
            'request_fingerprint': _preflight_request_fingerprint(request),
            'outcome': 'ready',
            'code': 'preflight_ready',
            'evidence_digest': _DIGEST,
        }
        if self.preflight_reports:
            report.update(self.preflight_reports.pop(0))
        return report

    def ensure_started(self, request):
        self.sends.append(request)
        if self.send_hook is not None:
            self.send_hook(request)
        if self.send_error is not None:
            raise self.send_error
        if (
            self.expected_start_fence is not None
            and request.fence_epoch != self.expected_start_fence
        ):
            raise RuntimeError('/private/stale-fence')
        report = {
            'operation_id': request.preflight.operation_id,
            'goal_uuid': request.preflight.goal_uuid,
            'binding_digest': request.preflight.binding_digest,
            'fence_epoch': request.fence_epoch,
        }
        if self.send_reports:
            report.update(self.send_reports.pop(0))
            return report
        report.update({'status': 'accepted', 'evidence_digest': _OTHER_DIGEST})
        return report

    def observe_goal(self, query):
        self.observes.append(query)
        if self.observe_hook is not None:
            self.observe_hook(query)
        report = {
            'operation_id': query.operation_id,
            'goal_uuid': query.goal_uuid,
            'binding_digest': query.binding_digest,
            'fence_epoch': query.fence_epoch,
        }
        if self.observe_reports:
            report.update(self.observe_reports.pop(0))
            return report
        report.update({'status': 'active', 'evidence_digest': _OTHER_DIGEST})
        return report

    def cancel_goal(self, request):
        self.cancels.append(request)
        if self.cancel_hook is not None:
            self.cancel_hook(request)
        report = {
            'operation_id': request.operation_id,
            'goal_uuid': request.goal_uuid,
            'binding_digest': request.binding_digest,
            'fence_epoch': request.fence_epoch,
        }
        if self.cancel_reports:
            report.update(self.cancel_reports.pop(0))
            return report
        report.update({'status': 'canceled', 'evidence_digest': _OTHER_DIGEST})
        return report


def _controller(store, port, clock):
    return GazeboMonitorRoomNav2Controller(
        store,
        port,
        worker_id='worker-1',
        lease_seconds=20.0,
        clock=clock,
    )


def _prepared(tmp_path):
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(_request(), now=1.0)
    return store


def test_controller_uses_only_store_private_samples_for_nav2_ports(
    tmp_path,
):
    """Never accept caller coordinates or invent a goal UUID."""
    store = _prepared(tmp_path)
    port = _Port()
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)

    first = controller.drive_once('operation-1')
    assert first.state == 'preflighting'
    clock.value = 3.0
    second = controller.drive_once('operation-1')

    assert second.state == 'navigating'
    assert [
        (request.x_m, request.y_m) for request in port.preflights
    ] == [(1.0, 2.0)]
    assert [
        (request.preflight.x_m, request.preflight.y_m)
        for request in port.sends
    ] == [(1.0, 2.0)]
    assert port.sends[0].preflight.goal_uuid == first.current_goal_uuid
    assert port.sends[0].preflight.operation_id == 'operation-1'
    assert port.sends[0].preflight.binding_digest
    assert port.sends[0].fence_epoch == second.fence_epoch
    assert port.sends[0].lease_expires_at == second.lease_expires_at
    assert port.sends[0].deadline == second.deadline
    assert 'x_m' not in repr(port.sends[0])
    public = second.to_public_dict()
    assert 'x_mm' not in repr(public)
    assert public['physical_authorized'] is False


def test_default_clock_is_strict_suspend_inclusive_boottime(
    tmp_path,
    monkeypatch,
):
    """Production authority time includes Linux suspend intervals."""
    store = _prepared(tmp_path)
    calls = []

    def clock_gettime(clock_id):
        calls.append(clock_id)
        return 2.0

    monkeypatch.setattr(adapter_module.time, 'clock_gettime', clock_gettime)
    controller = GazeboMonitorRoomNav2Controller(
        store,
        _Port(),
        worker_id='worker-1',
        lease_seconds=20.0,
    )

    assert controller.drive_once('operation-1').state == 'preflighting'
    assert calls == [adapter_module.time.CLOCK_BOOTTIME]


def test_default_boottime_failure_is_content_free_and_chain_free(
    tmp_path,
    monkeypatch,
):
    """Clock failure never falls back or discloses provider content."""
    store = _prepared(tmp_path)

    def failed_clock(_clock_id):
        raise OSError('/private/clock-secret')

    monkeypatch.setattr(adapter_module.time, 'clock_gettime', failed_clock)
    controller = GazeboMonitorRoomNav2Controller(
        store,
        _Port(),
        worker_id='worker-1',
        lease_seconds=20.0,
    )
    with pytest.raises(GazeboMonitorRoomNav2AdapterError) as raised:
        controller.drive_once('operation-1')
    assert str(raised.value) == 'nav2_clock_unavailable'
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__traceback__ is None


@pytest.mark.parametrize(
    'failure',
    [OverflowError('/private/overflow'), TypeError('/private/type')],
)
def test_default_boottime_normalizes_all_ordinary_provider_errors(
    tmp_path,
    monkeypatch,
    failure,
):
    """No ordinary clock binding failure escapes the typed clock code."""
    store = _prepared(tmp_path)

    def failed_clock(_clock_id):
        raise failure

    monkeypatch.setattr(adapter_module.time, 'clock_gettime', failed_clock)
    controller = GazeboMonitorRoomNav2Controller(
        store,
        _Port(),
        worker_id='worker-1',
        lease_seconds=20.0,
    )
    with pytest.raises(GazeboMonitorRoomNav2AdapterError) as raised:
        controller.drive_once('operation-1')
    assert str(raised.value) == 'nav2_clock_unavailable'
    assert '/private' not in str(raised.value)


def test_all_port_requests_expose_exact_public_fingerprints_and_wire(
    tmp_path,
):
    """Ports need no private helper to bind every request and wire payload."""
    store = _prepared(tmp_path)
    port = _Port()
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    clock.value = 3.0
    controller.drive_once('operation-1')
    start = port.sends[0]
    preflight = start.preflight
    assert preflight.request_fingerprint == (
        _preflight_request_fingerprint(preflight)
    )
    assert len(preflight.request_fingerprint) == 64
    assert len(start.request_fingerprint) == 64
    assert len(start.wire_payload_digest) == 64
    assert replace(preflight, x_m=1).request_fingerprint == (
        preflight.request_fingerprint
    )
    assert replace(start, fence_epoch=2).request_fingerprint != (
        start.request_fingerprint
    )
    assert replace(start, fence_epoch=2).wire_payload_digest == (
        start.wire_payload_digest
    )
    assert preflight.physical_authorized is False
    assert preflight.camera_coverage_validated is False

    clock.value = 4.0
    controller.drive_once('operation-1')
    query = port.observes[0]
    assert len(query.request_fingerprint) == 64
    cancel = Nav2CancelRequest(
        operation_id=query.operation_id,
        worker_id=query.worker_id,
        fence_epoch=query.fence_epoch,
        cancel_request_id='cancel-1',
        goal_uuid=query.goal_uuid,
        binding_digest=query.binding_digest,
    )
    assert len(cancel.request_fingerprint) == 64
    assert len(cancel.wire_payload_digest) == 64
    assert replace(
        cancel, cancel_request_id='cancel-2'
    ).request_fingerprint != cancel.request_fingerprint
    assert replace(
        cancel, cancel_request_id='cancel-2'
    ).wire_payload_digest == cancel.wire_payload_digest
    mutated_start = replace(start)
    object.__setattr__(mutated_start, 'fence_epoch', True)
    with pytest.raises(GazeboMonitorRoomNav2AdapterError):
        _ = mutated_start.request_fingerprint
    mutated_query = replace(query)
    object.__setattr__(mutated_query, 'worker_id', True)
    with pytest.raises(GazeboMonitorRoomNav2AdapterError):
        _ = mutated_query.request_fingerprint
    mutated_cancel = replace(cancel)
    object.__setattr__(mutated_cancel, 'cancel_request_id', True)
    with pytest.raises(GazeboMonitorRoomNav2AdapterError):
        _ = mutated_cancel.request_fingerprint
    mutated_preflight = replace(preflight)
    object.__setattr__(mutated_preflight, 'physical_authorized', 0)
    with pytest.raises(GazeboMonitorRoomNav2AdapterError):
        _ = mutated_preflight.request_fingerprint


@pytest.mark.parametrize(
    'outcome,code,expected_state,expected_terminal',
    [
        ('retryable', 'preflight_retryable', 'preflighting', None),
        ('rejected', 'preflight_rejected', 'failed', 'preflight_rejected'),
    ],
)
def test_typed_preflight_outcome_is_retryable_or_terminal_exactly(
    tmp_path,
    outcome,
    code,
    expected_state,
    expected_terminal,
):
    """Only ready can start; retryable waits and rejected fails target."""
    store = _prepared(tmp_path)
    port = _Port()
    port.preflight_reports.append({'outcome': outcome, 'code': code})
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    clock.value = 3.0

    result = controller.drive_once('operation-1')

    assert result.state == expected_state
    assert result.terminal_code == expected_terminal
    assert store.private_current_sample('operation-1').state == (
        'preflighting' if outcome == 'retryable' else 'failed'
    )
    assert port.sends == []


@pytest.mark.parametrize(
    'unsafe_code',
    ['private_secret_marker', _MaliciousStatus('preflight_rejected')],
)
def test_preflight_code_is_fixed_content_free_and_chain_free(
    tmp_path,
    unsafe_code,
):
    """Safe-shaped or string-subclass port content cannot become durable."""
    store = _prepared(tmp_path)
    port = _Port()
    port.preflight_reports.append(
        {'outcome': 'rejected', 'code': unsafe_code}
    )
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    clock.value = 3.0

    with pytest.raises(GazeboMonitorRoomNav2AdapterError) as raised:
        controller.drive_once('operation-1')

    assert str(raised.value) == 'nav2_invalid_code'
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__traceback__ is None
    assert store.observe('operation-1').state == 'preflighting'
    assert 'private_secret_marker' not in repr(store.events('operation-1'))
    assert port.sends == []


def test_preflight_code_mapping_is_process_immutable():
    """Injected code cannot widen the fixed outcome-code projection."""
    with pytest.raises(TypeError):
        adapter_module._PREFLIGHT_CODES['rejected'] = 'private_marker'


def test_two_sample_success_advances_without_coverage_claim(tmp_path):
    """Observed success advances samples but never asserts room coverage."""
    store = _prepared(tmp_path)
    port = _Port()
    clock = _Clock(2.0)
    controller = GazeboMonitorRoomNav2Controller(
        store,
        port,
        worker_id='worker-1',
        lease_seconds=200.0,
        clock=clock,
    )

    controller.drive_once('operation-1')
    clock.value = 3.0
    controller.drive_once('operation-1')
    port.observe_reports.append(
        {'status': 'succeeded', 'evidence_digest': _OTHER_DIGEST}
    )
    clock.value = 4.0
    reached = controller.drive_once('operation-1')

    assert reached.state == 'preflighting'
    assert reached.navigation_samples_reached == 1
    assert reached.coverage_achieved is False
    assert reached.camera_coverage_validated is False


def test_send_intent_crash_window_never_resends_after_restart(tmp_path):
    """After send intent, recovery observes or marks unknown, not resend."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(), now=1.0)
    port = _Port()
    port.send_error = RuntimeError('/private/nav2/send')
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)

    controller.drive_once('operation-1')
    clock.value = 3.0
    unknown = controller.drive_once('operation-1')

    assert unknown.state == 'delivery_unknown'
    assert len(port.sends) == 1
    store.close()

    reopened = GazeboMonitorRoomStore(path)
    recovery_port = _Port()
    recovery_port.observe_reports.append(
        {'status': 'unknown', 'evidence_digest': _OTHER_DIGEST}
    )
    recovered = _controller(
        reopened, recovery_port, _Clock(4.0)
    ).drive_once('operation-1')

    assert recovered.state == 'delivery_unknown'
    assert recovery_port.sends == []
    assert recovery_port.observes == []


@pytest.mark.parametrize(
    'observed_status,expected_state,deadline,restart_at,restart_worker',
    [
        ('active', 'navigating', 100.0, 5.0, 'worker-1'),
        ('unknown', 'delivery_unknown', 100.0, 5.0, 'worker-1'),
        ('active', 'navigating', 10.0, 11.0, 'worker-1'),
        ('active', 'navigating', 100.0, 22.0, 'worker-2'),
    ],
)
def test_committed_start_claim_restart_observes_and_never_resends(
    tmp_path,
    observed_status,
    expected_state,
    deadline,
    restart_at,
    restart_worker,
):
    """A crash after claim is reconciled by exact UUID observation only."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(deadline=deadline), now=1.0)
    port = _Port()
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    preflighting = controller.drive_once('operation-1')
    binding = store.private_operation_binding('operation-1')
    sample = store.private_current_sample('operation-1')
    send_intent = store.record_send_intent(
        store.transition_token('operation-1', worker_id='worker-1'),
        preflight_digest=_DIGEST,
        now=3.0,
    )
    start = Nav2StartRequest(
        preflight=controller._preflight_request(binding, sample),
        worker_id='worker-1',
        fence_epoch=send_intent.fence_epoch,
        lease_expires_at=send_intent.lease_expires_at,
        deadline=send_intent.deadline,
        preflight_digest=_DIGEST,
    )
    assert preflighting.current_goal_uuid == start.preflight.goal_uuid
    assert store.claim_start_dispatch(
        store.transition_token('operation-1', worker_id='worker-1'),
        start_fingerprint=start.request_fingerprint,
        binding_digest=binding.binding_digest,
        preflight_digest=_DIGEST,
        wire_payload_digest=start.wire_payload_digest,
        now=4.0,
    ) is True
    store.close()

    reopened = GazeboMonitorRoomStore(path)
    restarted_port = _Port()
    restarted_port.observe_reports.append(
        {'status': observed_status, 'evidence_digest': _OTHER_DIGEST}
    )
    result = GazeboMonitorRoomNav2Controller(
        reopened,
        restarted_port,
        worker_id=restart_worker,
        lease_seconds=20.0,
        clock=_Clock(restart_at),
    ).drive_once('operation-1')

    assert result.state == expected_state
    assert restarted_port.sends == []
    assert len(restarted_port.observes) == 1
    assert restarted_port.observes[0].goal_uuid == (
        send_intent.current_goal_uuid
    )


def test_send_unknown_maps_to_delivery_unknown_without_resend(tmp_path):
    """A content-backed send ambiguity is durable and never retried."""
    store = _prepared(tmp_path)
    port = _Port()
    port.send_reports.append(
        {'status': 'unknown', 'evidence_digest': _OTHER_DIGEST}
    )
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)

    controller.drive_once('operation-1')
    clock.value = 3.0
    unknown = controller.drive_once('operation-1')
    again = controller.drive_once('operation-1')

    assert unknown.state == 'delivery_unknown'
    assert again.state == 'delivery_unknown'
    assert len(port.sends) == 1
    assert port.observes == []


def test_preflight_time_crossing_deadline_blocks_send(tmp_path):
    """Refresh clock after preflight before opening the send side effect."""
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(_request(deadline=100.0), now=1.0)
    port = _Port()
    clock = _Clock(2.0)
    controller = GazeboMonitorRoomNav2Controller(
        store,
        port,
        worker_id='worker-1',
        lease_seconds=200.0,
        clock=clock,
    )
    controller.drive_once('operation-1')
    clock.value = 3.0

    def cross_deadline(_request):
        clock.value = 101.0

    port.preflight_hook = cross_deadline
    failed = controller.drive_once('operation-1')

    assert failed.state == 'failed'
    assert failed.terminal_code == 'deadline_expired'
    assert len(port.preflights) == 1
    assert port.sends == []


def test_post_intent_deadline_blocks_start_without_port_call(
    tmp_path,
    monkeypatch,
):
    """Deadline crossing after send-intent cannot open ensure_started."""
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(_request(deadline=100.0), now=1.0)
    port = _Port()
    clock = _Clock(2.0)
    controller = GazeboMonitorRoomNav2Controller(
        store,
        port,
        worker_id='worker-1',
        lease_seconds=200.0,
        clock=clock,
    )
    controller.drive_once('operation-1')
    clock.value = 3.0
    original = store.private_current_sample
    calls = {'count': 0}

    def advance_on_post_intent_sample(operation_id):
        calls['count'] += 1
        sample = original(operation_id)
        if calls['count'] == 2:
            clock.value = 101.0
        return sample

    monkeypatch.setattr(
        store,
        'private_current_sample',
        advance_on_post_intent_sample,
    )
    failed = controller.drive_once('operation-1')

    assert failed.state == 'failed'
    assert failed.terminal_code == 'deadline_expired'
    assert len(port.preflights) == 1
    assert port.sends == []


def test_post_intent_lease_expiry_leaves_send_intent_without_start(
    tmp_path,
    monkeypatch,
):
    """Lease expiry after send-intent does not start under a stale fence."""
    store = _prepared(tmp_path)
    port = _Port()
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    clock.value = 3.0
    original = store.private_current_sample
    calls = {'count': 0}

    def advance_on_post_intent_sample(operation_id):
        calls['count'] += 1
        sample = original(operation_id)
        if calls['count'] == 2:
            clock.value = 24.0
        return sample

    monkeypatch.setattr(
        store,
        'private_current_sample',
        advance_on_post_intent_sample,
    )
    send_intent = controller.drive_once('operation-1')

    assert send_intent.state == 'send_intent'
    assert send_intent.fence_epoch == 1
    assert len(port.preflights) == 1
    assert port.sends == []


def test_post_intent_clock_rollback_blocks_start_before_port(
    tmp_path,
    monkeypatch,
):
    """Clock rollback after send-intent is caught before ensure_started."""
    store = _prepared(tmp_path)
    port = _Port()
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    clock.value = 3.0
    original = store.private_current_sample
    calls = {'count': 0}

    def rollback_on_post_intent_sample(operation_id):
        calls['count'] += 1
        sample = original(operation_id)
        if calls['count'] == 2:
            clock.value = 2.5
        return sample

    monkeypatch.setattr(
        store,
        'private_current_sample',
        rollback_on_post_intent_sample,
    )
    with pytest.raises(GazeboMonitorRoomNav2AdapterError) as raised:
        controller.drive_once('operation-1')

    assert str(raised.value) == 'nav2_store_rejected'
    assert store.observe('operation-1').state == 'send_intent'
    assert port.sends == []

    with pytest.raises(GazeboMonitorRoomClockRollbackError):
        store.assert_start_ready(
            store.transition_token('operation-1', worker_id='worker-1'),
            now=2.5,
        )


def test_slow_preflight_discards_result_after_lease_takeover(tmp_path):
    """A slow preflight is retried under the next fence before send."""
    store = _prepared(tmp_path)
    port = _Port()
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    first = controller.drive_once('operation-1')
    assert first.fence_epoch == 1
    clock.value = 3.0

    def expire_lease_before_start(_request):
        clock.value = 23.0

    port.expected_start_fence = 2
    port.preflight_hook = expire_lease_before_start
    still_preflighting = controller.drive_once('operation-1')

    assert still_preflighting.state == 'preflighting'
    assert still_preflighting.fence_epoch == 2
    assert port.sends == []

    port.preflight_hook = None
    clock.value = 24.0
    navigating = controller.drive_once('operation-1')

    assert navigating.state == 'navigating'
    assert len(port.preflights) == 2
    assert port.sends[0].fence_epoch == 2


def test_stale_start_command_rejection_leaves_send_intent_for_reconcile(
    tmp_path,
):
    """A port-level stale fence rejection does not trigger a duplicate send."""
    store = _prepared(tmp_path)
    port = _Port()
    port.expected_start_fence = 999
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    clock.value = 3.0

    unknown = controller.drive_once('operation-1')

    assert unknown.state == 'delivery_unknown'
    assert len(port.sends) == 1


def test_port_cannot_mutate_operation_binding_during_preflight(tmp_path):
    """Frozen-object bypass in an injected port is detected before send."""
    store = _prepared(tmp_path)
    port = _Port()
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    clock.value = 3.0

    def mutate(request):
        object.__setattr__(request, 'map_revision', 'forged')

    port.preflight_hook = mutate
    with pytest.raises(GazeboMonitorRoomNav2AdapterError) as raised:
        controller.drive_once('operation-1')

    assert str(raised.value) == 'nav2_binding_changed'
    assert port.sends == []


def test_port_cannot_mutate_sample_coordinates_during_preflight(tmp_path):
    """A forged preflight point cannot be paired with the stored sample."""
    store = _prepared(tmp_path)
    port = _Port()
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    clock.value = 3.0

    def mutate(request):
        object.__setattr__(request, 'x_m', 9.0)

    port.preflight_hook = mutate
    with pytest.raises(GazeboMonitorRoomNav2AdapterError) as raised:
        controller.drive_once('operation-1')

    assert str(raised.value) == 'nav2_binding_changed'
    assert port.sends == []


def test_start_report_must_echo_exact_goal_binding_and_fence(tmp_path):
    """A terminal report for another goal is delivery-unknown evidence."""
    store = _prepared(tmp_path)
    port = _Port()
    port.send_reports.append({'goal_uuid': '0' * 32, 'status': 'accepted'})
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    clock.value = 3.0

    unknown = controller.drive_once('operation-1')

    assert unknown.state == 'delivery_unknown'
    assert len(port.sends) == 1


def test_cached_preflight_report_for_other_operation_cannot_start(tmp_path):
    """A preflight report cached for B cannot be reused for A."""
    store = _prepared(tmp_path)
    port = _Port()
    port.preflight_reports.append(
        {
            'operation_id': 'operation-2',
            'goal_uuid': '0' * 32,
            'binding_digest': _OTHER_DIGEST,
            'request_fingerprint': _OTHER_DIGEST,
            'evidence_digest': _OTHER_DIGEST,
        }
    )
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    clock.value = 3.0

    with pytest.raises(GazeboMonitorRoomNav2AdapterError) as raised:
        controller.drive_once('operation-1')

    assert str(raised.value) == 'nav2_goal_not_observable'
    assert store.observe('operation-1').state == 'preflighting'
    assert port.sends == []


def test_report_fence_rejects_bool_alias(tmp_path):
    """Boolean fence aliases cannot satisfy an integer fence echo."""
    store = _prepared(tmp_path)
    port = _Port()
    port.send_reports.append({'fence_epoch': True, 'status': 'accepted'})
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    clock.value = 3.0

    unknown = controller.drive_once('operation-1')

    assert unknown.state == 'delivery_unknown'
    assert len(port.sends) == 1


def test_report_status_rejects_str_subclass_without_persisting_content(
    tmp_path,
):
    """A status string subclass cannot smuggle formatted terminal text."""
    store = _prepared(tmp_path)
    port = _Port()
    port.send_reports.append(
        {
            'status': _MaliciousStatus('aborted'),
            'evidence_digest': _OTHER_DIGEST,
        }
    )
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    clock.value = 3.0

    unknown = controller.drive_once('operation-1')

    assert unknown.state == 'delivery_unknown'
    assert 'private_secret_marker' not in repr(store.events('operation-1'))


def test_observed_success_from_send_intent_records_acceptance_then_result(
    tmp_path,
):
    """A late observe can reconcile a completed goal without resending."""
    store = _prepared(tmp_path)
    port = _Port()
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    port.send_error = RuntimeError('/private/nav2/send')
    clock.value = 3.0
    assert controller.drive_once('operation-1').state == 'delivery_unknown'

    port = _Port()
    port.observe_reports.append(
        {'status': 'succeeded', 'evidence_digest': _OTHER_DIGEST}
    )
    reconciled = _controller(store, port, _Clock(4.0)).drive_once(
        'operation-1'
    )

    assert reconciled.state == 'delivery_unknown'
    assert reconciled.navigation_samples_reached == 0
    assert port.sends == []


def test_cancel_before_send_needs_no_nav2_cancel_call(tmp_path):
    """Pre-send cancel closes locally without external terminal evidence."""
    store = _prepared(tmp_path)
    port = _Port()
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    request = CancelOperation(
        cancel_request_id='cancel-1',
        transition=store.transition_token(
            'operation-1',
            worker_id='worker-1',
        ),
    )
    store.request_cancel(request, now=3.0)
    clock.value = 4.0

    canceled = controller.drive_once('operation-1')

    assert canceled.state == 'canceled'
    assert port.cancels == []


def test_public_cancel_once_closes_pre_send_operation_idempotently(tmp_path):
    """The gateway-facing cancel API never needs private controller state."""
    store = _prepared(tmp_path)
    port = _Port()
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)

    canceled = controller.cancel_once('operation-1', 'cancel-1')
    replay = controller.cancel_once('operation-1', 'cancel-1')

    assert canceled.state == 'canceled'
    assert replay.state == 'canceled'
    assert canceled.cancel_request_id == 'cancel-1'
    assert port.preflights == []
    assert port.sends == []
    assert port.cancels == []


def test_public_cancel_once_drives_exact_active_goal_cancel(tmp_path):
    """An active operation records intent before one exact port cancel."""
    store = _prepared(tmp_path)
    port = _Port()
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    clock.value = 3.0
    assert controller.drive_once('operation-1').state == 'navigating'
    clock.value = 4.0

    canceled = controller.cancel_once('operation-1', 'cancel-1')
    replay = controller.cancel_once('operation-1', 'cancel-1')

    assert canceled.state == 'canceled'
    assert replay.state == 'canceled'
    assert len(port.cancels) == 1
    assert port.cancels[0].cancel_request_id == 'cancel-1'


def test_public_cancel_once_rejects_changed_pending_identity(tmp_path):
    """A different gateway request cannot take over a pending cancel."""
    store = _prepared(tmp_path)
    port = _Port()
    port.cancel_reports.append({
        'status': 'active',
        'evidence_digest': _OTHER_DIGEST,
    })
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    clock.value = 3.0
    assert controller.drive_once('operation-1').state == 'navigating'
    clock.value = 4.0
    assert controller.cancel_once(
        'operation-1', 'cancel-1'
    ).state == 'cancel_requested'

    with pytest.raises(GazeboMonitorRoomNav2AdapterError) as error:
        controller.cancel_once('operation-1', 'cancel-2')

    assert error.value.code == 'nav2_state_rejected'
    assert len(port.cancels) == 1


def test_active_cancel_requires_port_terminal_evidence(tmp_path):
    """A sent goal cancel is mapped through the injected Nav2 port."""
    store = _prepared(tmp_path)
    port = _Port()
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    clock.value = 3.0
    controller.drive_once('operation-1')
    store.request_cancel(
        CancelOperation(
            cancel_request_id='cancel-1',
            transition=store.transition_token(
                'operation-1',
                worker_id='worker-1',
            ),
        ),
        now=4.0,
    )
    clock.value = 5.0

    canceled = controller.drive_once('operation-1')

    assert canceled.state == 'canceled'
    assert len(port.cancels) == 1
    assert port.cancels[0].fence_epoch == canceled.fence_epoch
    assert port.cancels[0].goal_uuid == canceled.current_goal_uuid
    assert port.cancels[0].cancel_request_id == 'cancel-1'


@pytest.mark.parametrize(
    'observed_status,expected_state,restart_at,restart_worker',
    [
        ('canceled', 'canceled', 6.0, 'worker-1'),
        ('unknown', 'cancel_unknown', 6.0, 'worker-1'),
        ('canceled', 'canceled', 23.0, 'worker-2'),
    ],
)
def test_committed_cancel_claim_restart_observes_and_never_recancels(
    tmp_path,
    observed_status,
    expected_state,
    restart_at,
    restart_worker,
):
    """A crash after cancel claim can only observe the exact stable goal."""
    path = tmp_path / 'state.sqlite3'
    store = GazeboMonitorRoomStore(path)
    store.prepare(_request(), now=1.0)
    port = _Port()
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    clock.value = 3.0
    navigating = controller.drive_once('operation-1')
    canceled_intent = store.request_cancel(
        CancelOperation(
            cancel_request_id='cancel-1',
            transition=store.transition_token(
                'operation-1', worker_id='worker-1'
            ),
        ),
        now=4.0,
    )
    binding = store.private_operation_binding('operation-1')
    request = Nav2CancelRequest(
        operation_id='operation-1',
        worker_id='worker-1',
        fence_epoch=canceled_intent.fence_epoch,
        cancel_request_id='cancel-1',
        goal_uuid=canceled_intent.current_goal_uuid,
        binding_digest=binding.binding_digest,
    )
    assert store.claim_cancel_dispatch(
        store.transition_token('operation-1', worker_id='worker-1'),
        cancel_request_id='cancel-1',
        request_fingerprint=request.request_fingerprint,
        binding_digest=binding.binding_digest,
        wire_payload_digest=request.wire_payload_digest,
        now=5.0,
    ) is True
    store.close()

    reopened = GazeboMonitorRoomStore(path)
    restarted_port = _Port()
    restarted_port.observe_reports.append(
        {'status': observed_status, 'evidence_digest': _OTHER_DIGEST}
    )
    result = GazeboMonitorRoomNav2Controller(
        reopened,
        restarted_port,
        worker_id=restart_worker,
        lease_seconds=20.0,
        clock=_Clock(restart_at),
    ).drive_once('operation-1')

    assert result.state == expected_state
    assert restarted_port.cancels == []
    assert len(restarted_port.observes) == 1
    assert restarted_port.observes[0].goal_uuid == (
        navigating.current_goal_uuid
    )


def test_concurrent_drive_does_not_duplicate_external_preflight(tmp_path):
    """A controller-instance reservation coalesces duplicate preflight."""
    store = _prepared(tmp_path)
    port = _Port()
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    clock.value = 3.0
    entered = Event()
    release = Event()

    def wait_inside_preflight(_request):
        entered.set()
        release.wait(5.0)

    port.preflight_hook = wait_inside_preflight
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(controller.drive_once, 'operation-1')
        assert entered.wait(5.0)
        second = executor.submit(controller.drive_once, 'operation-1')
        with pytest.raises(GazeboMonitorRoomNav2AdapterError) as raised:
            second.result(timeout=5.0)
        release.set()
        assert first.result(timeout=5.0).state == 'navigating'

    assert str(raised.value) == 'nav2_external_call_in_progress'
    assert len(port.preflights) == 1
    assert len(port.sends) == 1


@pytest.mark.parametrize('port_raises', [False, True])
def test_stale_preflight_result_cannot_mutate_next_sample(
    tmp_path,
    port_raises,
):
    """A stale preflight report or exception cannot alter sample one."""
    store = _prepared(tmp_path)
    clock = _Clock(2.0)
    stale_port = _Port()
    advancing_port = _Port()
    stale = _controller(store, stale_port, clock)
    advancing = _controller(store, advancing_port, clock)
    stale.drive_once('operation-1')
    clock.value = 3.0
    entered = Event()
    release = Event()

    def block_preflight(_request):
        entered.set()
        release.wait(5.0)
        if port_raises:
            raise RuntimeError('/private/stale-preflight')

    stale_port.preflight_hook = block_preflight
    with ThreadPoolExecutor(max_workers=2) as executor:
        old = executor.submit(stale.drive_once, 'operation-1')
        assert entered.wait(5.0)
        assert advancing.drive_once('operation-1').state == 'navigating'
        advancing_port.observe_reports.append(
            {'status': 'succeeded', 'evidence_digest': _OTHER_DIGEST}
        )
        advanced = advancing.drive_once('operation-1')
        assert advanced.state == 'preflighting'
        assert advanced.current_sample_index == 1
        release.set()
        result = old.result(timeout=5.0)

    assert result.state == 'preflighting'
    assert result.current_sample_index == 1
    assert stale_port.sends == []
    assert store.private_current_sample('operation-1').state == 'preflighting'


@pytest.mark.parametrize('port_raises', [False, True])
def test_stale_start_result_cannot_mutate_next_send_intent(
    tmp_path,
    port_raises,
):
    """A stale start report or exception cannot poison the next intent."""
    store = _prepared(tmp_path)
    clock = _Clock(2.0)
    stale_port = _Port()
    advancing_port = _Port()
    stale = _controller(store, stale_port, clock)
    advancing = _controller(store, advancing_port, clock)
    stale.drive_once('operation-1')
    clock.value = 3.0
    stale_entered = Event()
    stale_release = Event()
    next_entered = Event()
    next_release = Event()

    def block_stale_start(request):
        if request.preflight.sample_index == 0:
            stale_entered.set()
            stale_release.wait(5.0)

    def block_next_start(request):
        if request.preflight.sample_index == 1:
            next_entered.set()
            next_release.wait(5.0)

    stale_port.send_hook = block_stale_start
    if port_raises:
        stale_port.send_error = RuntimeError('/private/stale-start')
    advancing_port.send_hook = block_next_start
    with ThreadPoolExecutor(max_workers=2) as executor:
        old = executor.submit(stale.drive_once, 'operation-1')
        assert stale_entered.wait(5.0)
        advancing_port.observe_reports.append(
            {'status': 'succeeded', 'evidence_digest': _OTHER_DIGEST}
        )
        advanced = advancing.drive_once('operation-1')
        assert advanced.state == 'preflighting'
        assert advanced.current_sample_index == 1
        next_start = executor.submit(
            advancing.drive_once, 'operation-1'
        )
        assert next_entered.wait(5.0)
        stale_release.set()
        result = old.result(timeout=5.0)

        assert result.state == 'send_intent'
        assert result.current_sample_index == 1
        assert store.private_current_sample('operation-1').state == (
            'send_intent'
        )
        next_release.set()
        assert next_start.result(timeout=5.0).state == 'navigating'


@pytest.mark.parametrize('port_raises', [False, True])
def test_stale_observe_result_cannot_fail_next_sample(
    tmp_path,
    port_raises,
):
    """A stale observe report or exception cannot fail sample one."""
    store = _prepared(tmp_path)
    clock = _Clock(2.0)
    setup_port = _Port()
    setup = _controller(store, setup_port, clock)
    setup.drive_once('operation-1')
    clock.value = 3.0
    assert setup.drive_once('operation-1').state == 'navigating'
    stale_port = _Port()
    advancing_port = _Port()
    stale_port.observe_reports.append(
        {'status': 'aborted', 'evidence_digest': _OTHER_DIGEST}
    )
    advancing_port.observe_reports.append(
        {'status': 'succeeded', 'evidence_digest': _OTHER_DIGEST}
    )
    stale = _controller(store, stale_port, clock)
    advancing = _controller(store, advancing_port, clock)
    entered = Event()
    release = Event()

    def block_observe(_query):
        entered.set()
        release.wait(5.0)
        if port_raises:
            raise RuntimeError('/private/stale-observe')

    stale_port.observe_hook = block_observe
    with ThreadPoolExecutor(max_workers=2) as executor:
        old = executor.submit(stale.drive_once, 'operation-1')
        assert entered.wait(5.0)
        advanced = advancing.drive_once('operation-1')
        assert advanced.state == 'preflighting'
        assert advanced.current_sample_index == 1
        release.set()
        result = old.result(timeout=5.0)

    assert result.state == 'preflighting'
    assert result.current_sample_index == 1
    assert result.terminal_code is None
    assert store.private_current_sample('operation-1').state == 'preflighting'


def test_succeeded_two_step_cas_cannot_complete_next_sample(
    tmp_path,
    monkeypatch,
):
    """A race between acceptance and success keeps the old goal token."""
    store = _prepared(tmp_path)
    clock = _Clock(2.0)
    setup = _controller(store, _Port(), clock)
    setup.drive_once('operation-1')
    store.record_send_intent(
        store.transition_token('operation-1', worker_id='worker-1'),
        preflight_digest=_DIGEST,
        now=3.0,
    )
    clock.value = 4.0
    stale_port = _Port()
    stale_port.observe_reports.append(
        {'status': 'succeeded', 'evidence_digest': _OTHER_DIGEST}
    )
    advancing_port = _Port()
    advancing_port.observe_reports.append(
        {'status': 'succeeded', 'evidence_digest': _OTHER_DIGEST}
    )
    stale = _controller(store, stale_port, clock)
    advancing = _controller(store, advancing_port, clock)
    accepted = Event()
    release = Event()
    original = store.record_navigating
    calls = {'count': 0}

    def block_after_acceptance(*args, **kwargs):
        result = original(*args, **kwargs)
        calls['count'] += 1
        if calls['count'] == 1:
            accepted.set()
            release.wait(5.0)
        return result

    monkeypatch.setattr(store, 'record_navigating', block_after_acceptance)
    with ThreadPoolExecutor(max_workers=2) as executor:
        old = executor.submit(stale.drive_once, 'operation-1')
        assert accepted.wait(5.0)
        advanced = advancing.drive_once('operation-1')
        assert advanced.state == 'preflighting'
        assert advanced.current_sample_index == 1
        release.set()
        result = old.result(timeout=5.0)

    assert result.state == 'preflighting'
    assert result.current_sample_index == 1
    assert result.navigation_samples_reached == 1
    assert store.private_current_sample('operation-1').state == 'preflighting'


def test_cancel_reservation_spans_terminal_recording(tmp_path, monkeypatch):
    """A second thread cannot duplicate cancel after the port returns."""
    store = _prepared(tmp_path)
    port = _Port()
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    clock.value = 3.0
    controller.drive_once('operation-1')
    store.request_cancel(
        CancelOperation(
            cancel_request_id='cancel-1',
            transition=store.transition_token(
                'operation-1',
                worker_id='worker-1',
            ),
        ),
        now=4.0,
    )
    clock.value = 5.0
    entered = Event()
    release = Event()
    original = GazeboMonitorRoomNav2Controller._assert_cancel_report_target

    def blocked_assert(report, request):
        entered.set()
        release.wait(5.0)
        return original(report, request)

    monkeypatch.setattr(
        GazeboMonitorRoomNav2Controller,
        '_assert_cancel_report_target',
        staticmethod(blocked_assert),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(controller.drive_once, 'operation-1')
        assert entered.wait(5.0)
        second = executor.submit(controller.drive_once, 'operation-1')
        assert second.result(timeout=5.0).state == 'cancel_requested'
        release.set()
        assert first.result(timeout=5.0).state == 'canceled'

    assert len(port.cancels) == 1
    assert len(port.observes) == 1


@pytest.mark.parametrize('first_cancel_raises', [False, True])
def test_cross_instance_cancel_requests_share_exact_idempotency_key(
    tmp_path,
    first_cancel_raises,
):
    """A durable claim suppresses cross-controller cancel resends."""
    store = _prepared(tmp_path)
    port = _Port()
    clock = _Clock(2.0)
    first_controller = _controller(store, port, clock)
    second_controller = _controller(store, port, clock)
    first_controller.drive_once('operation-1')
    clock.value = 3.0
    first_controller.drive_once('operation-1')
    store.request_cancel(
        CancelOperation(
            cancel_request_id='cancel-1',
            transition=store.transition_token(
                'operation-1',
                worker_id='worker-1',
            ),
        ),
        now=4.0,
    )
    clock.value = 5.0
    entered = Event()
    release = Event()
    calls = {'count': 0}

    def block_first_cancel(_request):
        calls['count'] += 1
        if calls['count'] == 1:
            entered.set()
            release.wait(5.0)
            if first_cancel_raises:
                raise RuntimeError('/private/stale-cancel')

    port.cancel_hook = block_first_cancel
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_controller.drive_once, 'operation-1')
        assert entered.wait(5.0)
        second = executor.submit(second_controller.drive_once, 'operation-1')
        assert second.result(timeout=5.0).state == 'cancel_requested'
        release.set()
        assert first.result(timeout=5.0).state == (
            'cancel_unknown' if first_cancel_raises else 'canceled'
        )

    assert len(port.cancels) == 1
    assert len(port.observes) == 1
    assert {
        (
            request.cancel_request_id,
            request.operation_id,
            request.goal_uuid,
            request.fence_epoch,
            request.binding_digest,
        )
        for request in port.cancels
    } == {
        (
            'cancel-1',
            'operation-1',
            port.cancels[0].goal_uuid,
            port.cancels[0].fence_epoch,
            port.cancels[0].binding_digest,
        )
    }


def test_deadline_blocks_preflight_side_effects(tmp_path):
    """An expired preflight records failure without calling the port."""
    store = GazeboMonitorRoomStore(tmp_path / 'state.sqlite3')
    store.prepare(_request(deadline=5.0), now=1.0)
    port = _Port()
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    clock.value = 5.0

    failed = controller.drive_once('operation-1')

    assert failed.state == 'failed'
    assert failed.terminal_code == 'deadline_expired'
    assert port.preflights == []
    assert port.sends == []


def test_adapter_errors_are_content_free(tmp_path):
    """Port failures do not leak private messages or Python chains."""
    store = _prepared(tmp_path)
    port = _Port()
    port.send_error = RuntimeError('/private/nav2/goal')
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    clock.value = 3.0

    unknown = controller.drive_once('operation-1')

    assert unknown.state == 'delivery_unknown'
    assert '/private/nav2/goal' not in repr(store.events('operation-1'))


def test_port_supplied_adapter_error_code_is_allowlisted(tmp_path):
    """A malicious adapter error string is not echoed at the boundary."""
    store = _prepared(tmp_path)
    port = _Port()
    port.send_error = GazeboMonitorRoomNav2AdapterError(
        'nav2_/private/secret'
    )
    clock = _Clock(2.0)
    controller = _controller(store, port, clock)
    controller.drive_once('operation-1')
    clock.value = 3.0

    unknown = controller.drive_once('operation-1')

    assert unknown.state == 'delivery_unknown'
    assert '/private/secret' not in repr(store.events('operation-1'))


def test_constructor_numeric_overflow_is_content_free(tmp_path):
    """Unbounded numeric input is normalized into an adapter error."""
    store = _prepared(tmp_path)
    with pytest.raises(GazeboMonitorRoomNav2AdapterError) as raised:
        GazeboMonitorRoomNav2Controller(
            store,
            _Port(),
            worker_id='worker-1',
            lease_seconds=10 ** 10000,
        )

    assert str(raised.value) == 'nav2_invalid_lease_seconds'


def test_adapter_has_no_ros_or_nav2_imports():
    """The pure controller file never imports ROS or Nav2 modules."""
    tree = ast.parse(inspect.getsource(adapter_module))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)

    assert all(not name.startswith('rclpy') for name in imported)
    assert all(not name.startswith('nav2') for name in imported)
