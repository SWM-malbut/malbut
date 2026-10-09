"""Exercise preparation, replay and data boundaries without hardware or HTTPS."""

from concurrent.futures import Future, TimeoutError
import json
import queue
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from malbut_bringup.device_operations import DeviceOperations, validate_operation
from malbut_bringup.web_panel import OwnerCallTimeout, PanelData, RosBridge


@pytest.fixture
def operations(tmp_path):
    """Use the real journal and deterministic fake ROS owner."""
    bridge = SimpleNamespace(data=PanelData(), catalog=Mock(), runtime=Mock())
    bridge.data.system = {'movement_runtime_id': 'manager', 'movement_epoch': 0}
    bridge.runtime_stop_error = None
    bridge.stopping_runtime = None
    bridge.runtime_message = ''
    bridge.runtime.snapshot.side_effect = lambda: bridge.data.snapshot()['runtime']
    bridge.catalog.list_maps.return_value = [
        {'id': 'home.yaml', 'name': 'home', 'path': '/x/home.yaml'}]
    bridge.runtime.last_selected_map.return_value = 'home.yaml'
    bridge.call = lambda function, **_kwargs: function()
    bridge._refresh = Mock()
    bridge._start_runtime = Mock()
    bridge._stop_runtime = Mock()
    cloud = Mock()
    ops = DeviceOperations(bridge, cloud, tmp_path / 'operations.sqlite3')
    yield ops, bridge, cloud
    ops.close()


def test_unknown_send_after_restart_is_never_replayed(operations, tmp_path):
    """A crash between durable admission and response must not repeat an effect."""
    ops, bridge, cloud = operations
    ops.db.execute('INSERT INTO operations VALUES (?,?,?,?,NULL)',
                   ('ambiguous', 'homecam_settings', '{"cameraEnabled": false}', 'sent'))
    ops.db.commit()
    reopened = DeviceOperations(bridge, cloud, tmp_path / 'operations.sqlite3')
    try:
        result = reopened.execute('ambiguous', 'homecam_settings', {'cameraEnabled': False})
        assert result['code'] == 'result_unknown'
        cloud.request.assert_not_called()
    finally:
        reopened.close()


def test_same_id_is_idempotent_and_cannot_change_target(operations):
    """Cloud retries use the same idempotency ID and current backend authorization."""
    ops, _, cloud = operations
    cloud.request.return_value = {'success': True, 'code': 'saved',
                                  'result': {'application': 'pending'}, 'message': 'saved'}
    result = ops.execute('one', 'homecam_settings', {'cameraEnabled': False})
    assert ops.execute('one', 'homecam_settings', {'cameraEnabled': False}) == result
    conflict = ops.execute('one', 'homecam_settings', {'cameraEnabled': True})
    assert conflict['code'] == 'request_conflict'
    assert ops.execute('one', 'map_select', {'map': '/tmp/map.yaml'})['success'] is False
    assert ops.execute('one', 'homecam_settings', {'cameraEnabled': False}) == result
    assert cloud.request.call_count == 3
    cloud.request.assert_called_with('/api/device/v1/agent/operate', 'POST', {
        'requestId': 'one', 'operation': 'homecam_settings',
        'arguments': {'cameraEnabled': False}})


def test_cached_sensitive_reply_rechecks_revoked_delegation(operations):
    """A successful local receipt cannot bypass a later backend denial."""
    ops, _, cloud = operations
    cloud.request.return_value = {'success': True, 'code': 'OK',
                                  'result': {'events': ['private']}, 'message': 'loaded'}
    assert ops.execute('event-query', 'homecam_events', {})['success']
    cloud.request.return_value = {'success': False, 'code': 'VOICE_AGENT_DISABLED',
                                  'result': {}, 'message': 'delegation revoked'}
    result = ops.execute('event-query', 'homecam_events', {})
    assert not result['success'] and result['result'] == {}


@pytest.mark.parametrize('operation,args', [
    ('shell', {}), ('runtime_start', {'mode': 'navigation', 'map': '../home.yaml'}),
    ('map_select', {'map': 'home.yaml', 'ros_name': '/arbitrary'}),
    ('zones_update', {'map': 'home.yaml', 'index': 0, 'revision': 'x', 'points': [[1, 2]]}),
    ('homecam_status', {'huge': 'x' * 17000}),
])
def test_invalid_effects_cannot_reach_backend_or_ros(operations, operation, args):
    """No supplied command, file path or polygon expands the fixed interface."""
    ops, bridge, cloud = operations
    assert not ops.execute('invalid', operation, args)['success']
    bridge._start_runtime.assert_not_called()
    cloud.request.assert_not_called()


def test_map_delete_requires_explicit_confirmation_and_exact_target(operations):
    """Unconfirmed data deletion never reaches the existing catalog writer."""
    ops, bridge, _ = operations
    result = ops.execute('delete', 'map_delete', {'map': 'home.yaml'})
    assert result['code'] == 'confirmation_required'
    bridge.catalog.delete.assert_not_called()


def test_cancel_before_start_has_no_effect(operations):
    """A queued canceled preparation cannot launch a robot group later."""
    ops, bridge, _ = operations
    cancel = threading.Event()
    cancel.set()
    result = ops.execute('cancel', 'runtime_start', {'mode': 'mapping'}, cancel)
    assert result['code'] == 'canceled'
    bridge._start_runtime.assert_not_called()


def test_runtime_stop_rechecks_all_manager_ids(operations):
    """A different active mission appearing after confirmation remains running."""
    ops, bridge, _ = operations
    bridge.data.system = {'active_foreground_missions': [
        {'mission_id': 'new', 'capability_id': 'patrol'}]}
    result = ops.execute('stop', 'runtime_stop', {'confirmed_mission_ids': ['old']})
    assert result['code'] == 'preemption_confirmation_required'
    assert result['result']['conflicting_mission_ids'] == ['new']
    bridge._stop_runtime.assert_not_called()


def test_map_load_receipt_separates_pose_readiness(operations):
    """Successful file loading cannot invent successful pose recovery."""
    ops, bridge, _ = operations
    future = Future()
    future.set_result(SimpleNamespace(result=0))
    bridge._start_runtime.return_value = future
    bridge.data.runtime.update(state='RUNNING', mode='navigation', ready=True, localization={
        'mode': 'LOCALIZATION', 'map': '/maps/home.yaml', 'pose_ready': False,
        'runtime_id': 'lifetime', 'transition_id': 2})
    result = ops.execute('load', 'map_select', {
        'map': 'home.yaml', 'movement_runtime_id': 'manager', 'movement_epoch': 0})
    assert result['success']
    assert result['result']['localization']['pose_ready'] is False
    assert bridge._start_runtime.call_count == 1
    assert result['result']['preparation_movement_binding'] == {
        'runtime_id': 'manager', 'epoch': 0}


def test_localization_failure_does_not_report_success(operations):
    """The existing LoadMap error is terminal for this preparation stage."""
    ops, bridge, _ = operations
    future = Future()
    future.set_result(SimpleNamespace(result=3))
    bridge._start_runtime.return_value = future
    result = ops.execute('failed-map', 'map_select', {'map': 'home.yaml'})
    assert not result['success'] and result['code'] == 'localization_failed'


def test_status_keeps_transition_state_but_ages_sensor_evidence(operations, monkeypatch):
    """Latched map data is valid; old detections and absent battery are not live facts."""
    ops, bridge, _ = operations
    monkeypatch.setattr('malbut_bringup.device_operations.time.time', lambda: 100.0)
    bridge.data.runtime.update(state='RUNNING', localization={
        'map': '/x/home.yaml', 'pose_ready': True})
    bridge.data.person_observation = {'count': 1, 'observed_at': 90.0}
    bridge.data.tracking = 'track 17'
    bridge.data.tracking_observed_at = 99.0
    result = ops.execute('status', 'status', {})['result']
    assert result['runtime']['localization']['pose_ready']
    assert result['runtime']['localization']['map'] == 'home.yaml'
    assert result['observations']['person']['current'] is False
    assert result['tracking']['current'] is True
    assert result['battery'] is None
    assert result['maps'] == [{'id': 'home.yaml', 'name': 'home'}]
    assert result['last_selected_map'] == 'home.yaml'


def test_zone_update_reuses_geometry_and_rejects_changed_revision(operations, monkeypatch):
    """Voice may change existing attributes, never author coordinates or edit a stale map."""
    ops, bridge, _ = operations
    view = {'map': 'home.yaml', 'editable': True, 'message': '', 'zones': [
        {'name': 'sofa', 'behavior': 'allow', 'points': [[0, 0], [1, 0], [1, 1]]}]}
    monkeypatch.setattr('malbut_bringup.device_operations.zone_view',
                        lambda *_: json.loads(json.dumps(view)))
    save = Mock()
    monkeypatch.setattr('malbut_bringup.device_operations.save_zones', save)
    query = ops.execute('zones', 'zones_get', {})['result']
    args = {'map': 'home.yaml', 'index': 0, 'revision': query['revision'], 'behavior': 'avoid'}
    assert ops.execute('edit', 'zones_update', args)['success']
    assert save.call_args.args[2]['zones'][0]['points'] == view['zones'][0]['points']
    stale = ops.execute('stale', 'zones_update', {**args, 'revision': 'old'})
    assert stale['code'] == 'target_changed'
    assert save.call_count == 1


def test_local_validator_does_not_expose_arbitrary_cloud_operation():
    """A supported fixed operation is required even with a valid device token."""
    with pytest.raises(ValueError):
        validate_operation('/api/devices/other/settings', {})


@pytest.mark.parametrize('failed', [False, True])
def test_canceled_start_waits_for_owned_process_exit(operations, failed):
    """Cancel receipt must wait for cleanup; failed shutdown is explicitly unconfirmed."""
    ops, bridge, _ = operations
    cancel, cleanup_started = threading.Event(), threading.Event()
    pending, cleanup = Future(), Future()

    def start(_):
        cancel.set()
        return pending

    def stop():
        cleanup_started.set()
        return cleanup

    bridge._start_runtime.side_effect = start
    bridge.runtime.stop.side_effect = stop
    responses = []
    thread = threading.Thread(target=lambda: responses.append(ops.execute(
        'cancel-start', 'runtime_start', {'mode': 'mapping'}, cancel)))
    thread.start()
    assert cleanup_started.wait(2)
    assert thread.is_alive() and responses == []
    if failed:
        cleanup.set_exception(RuntimeError('process group remains'))
    else:
        cleanup.set_result({'state': 'STOPPED'})
    thread.join(2)
    assert not thread.is_alive()
    assert responses[0]['code'] == ('stop_unconfirmed' if failed else 'canceled')


@pytest.mark.parametrize('mode,map_path', [
    ('navigation', '/maps/home.yaml'), ('mapping', '/package/default_map.yaml'),
])
def test_new_runtime_waits_for_localization_transition_end(operations, mode, map_path):
    """Manager startup is not completion while its initial map transition runs."""
    ops, bridge, _ = operations
    bridge.data.runtime.update(state='RUNNING', ready=True, localization={
        'mode': 'SWITCHING', 'map': map_path, 'pose_ready': False})
    bridge._start_runtime.return_value = None
    responses = []
    arguments = {'mode': mode, 'movement_runtime_id': 'manager', 'movement_epoch': 0}
    if mode == 'navigation':
        arguments['map'] = 'home.yaml'
    thread = threading.Thread(target=lambda: responses.append(ops.execute(
        'start-navigation', 'runtime_start', arguments)))
    thread.start()
    import time
    time.sleep(0.2)
    assert thread.is_alive() and responses == []
    bridge.data.runtime['localization'].update(mode='LOCALIZATION', pose_ready=True)
    thread.join(2)
    assert not thread.is_alive()
    assert responses[0]['result']['localization']['pose_ready'] is True


@pytest.mark.parametrize('initially_stopped', [False, True])
def test_mapping_preparation_accepts_settled_default_map_without_starting_slam(
        operations, monkeypatch, initially_stopped):
    """The baseline runtime localizes on its unknown map; AutoSLAM owns SLAM later."""
    ops, bridge, _ = operations
    bridge.data.runtime['state'] = 'STOPPED' if initially_stopped else 'RUNNING'

    def start(_payload):
        bridge.data.runtime.update(state='RUNNING', mode='navigation', map=None, ready=True,
                                   localization={'mode': 'LOCALIZATION',
                                                 'map': '/package/default_map.yaml',
                                                 'pose_ready': True})

    bridge._start_runtime.side_effect = start

    def ready(predicate, _canceled):
        assert predicate(), 'preparation did not settle'

    monkeypatch.setattr(ops, '_wait', ready)
    arguments = {'mode': 'mapping'}
    if not initially_stopped:
        arguments.update(movement_runtime_id='manager', movement_epoch=0)
    result = ops.execute('default-start', 'runtime_start', arguments)
    assert result['success']
    assert result['result']['map'] is None
    assert result['result']['localization']['mode'] == 'LOCALIZATION'
    assert result['result']['localization']['map'] == 'default_map.yaml'
    assert result['result']['preparation_movement_binding'] == {
        'runtime_id': 'manager', 'epoch': 0}
    assert bridge._start_runtime.call_args.args[0]['mode'] == 'mapping'


@pytest.mark.parametrize('initially_stopped', [True, False])
@pytest.mark.parametrize('failed', [False, True])
def test_preparation_wait_timeout_requires_confirmed_cleanup(
        operations, monkeypatch, initially_stopped, failed):
    """Readiness/localization expiry cannot leave delayed movement unobserved."""
    ops, bridge, _ = operations
    bridge.data.runtime.update(
        state='STOPPED' if initially_stopped else 'RUNNING',
        ready=not initially_stopped, localization={'mode': 'SWITCHING'})
    bridge._start_runtime.return_value = None
    cleanup, cleanup_started = Future(), threading.Event()

    def stop(*_):
        cleanup_started.set()
        return cleanup

    bridge.runtime.stop.side_effect = stop
    bridge.stop_movement = Mock()
    bridge.stop_movement.service_is_ready.return_value = True
    bridge.stop_movement.call_async.side_effect = stop
    bridge.stop_movement_request = Mock()
    original_wait = ops._wait
    monkeypatch.setattr(ops, '_wait', lambda predicate, canceled, timeout=180.0:
                        original_wait(predicate, canceled, .01 if timeout == 180.0 else timeout))
    responses = []
    arguments = {'mode': 'mapping', 'movement_runtime_id': 'manager', 'movement_epoch': 0}
    thread = threading.Thread(target=lambda: responses.append(ops.execute(
        'readiness-timeout', 'runtime_start', arguments)))
    thread.start()
    assert cleanup_started.wait(2)
    assert thread.is_alive() and responses == []
    if failed:
        cleanup.set_exception(RuntimeError('Stop is unconfirmed'))
    else:
        cleanup.set_result(SimpleNamespace(stopped=True))
    thread.join(2)
    assert not thread.is_alive()
    assert responses[0]['code'] == ('stop_unconfirmed' if failed else 'result_unknown')
    assert bridge.runtime.stop.call_count == int(initially_stopped)
    assert bridge.stop_movement.call_async.call_count == int(not initially_stopped)
    assert bridge._start_runtime.call_count == 1


def test_shutdown_preserves_raced_manager_confirmation(operations):
    """A task arriving after initial state inspection returns its actual ID."""
    ops, bridge, _ = operations
    bridge.data.runtime['state'] = 'RUNNING'
    bridge.runtime_stop_error = {
        'code': 'preemption_confirmation_required', 'message': 'Confirm localization',
        'result': {'conflicting_mission_ids': ['localization:2']}}
    result = ops.execute('raced-stop', 'runtime_stop', {})
    assert result['code'] == 'preemption_confirmation_required'
    assert result['result']['conflicting_mission_ids'] == ['localization:2']


@pytest.mark.parametrize('initially_stopped', [True, False])
@pytest.mark.parametrize('localization_mode', ['MAPPING', 'LOCALIZATION'])
def test_stop_during_startup_cannot_refresh_preparation_epoch(
        operations, initially_stopped, localization_mode):
    """Even a first observation after stop cannot relabel old intent with a new epoch."""
    ops, bridge, _ = operations
    bridge.data.runtime['state'] = 'STOPPED' if initially_stopped else 'RUNNING'

    def start(_payload):
        bridge.data.runtime.update(state='RUNNING', mode='mapping', ready=True,
                                   localization={'mode': localization_mode})
        bridge.data.system['movement_epoch'] = 1
    bridge._start_runtime.side_effect = start
    result = ops.execute('startup-stop', 'runtime_start', {
        'mode': 'mapping', 'movement_runtime_id': 'manager', 'movement_epoch': 0})
    assert result['code'] == 'movement_epoch_changed'


def test_preparation_canceled_while_queued_never_starts_runtime(operations):
    """Recheck cancellation on the ROS owner, after admission on the worker."""
    ops, bridge, _ = operations
    cancel = threading.Event()

    def call(function, **_kwargs):
        cancel.set()
        return function()

    bridge.call = call
    result = ops.execute('queued-cancel', 'runtime_start', {'mode': 'mapping'}, cancel)
    assert result['code'] == 'canceled'
    bridge._start_runtime.assert_not_called()
    bridge.runtime.stop.assert_not_called()


@pytest.mark.parametrize('canceled', [False, True])
def test_preparation_dispatch_timeout_waits_for_cleanup(operations, canceled):
    """An effect that started before dispatch timeout needs a verified shutdown."""
    ops, bridge, _ = operations
    cancel, cleanup_started = threading.Event(), threading.Event()
    cleanup = Future()
    calls = []

    def call(function, **_kwargs):
        calls.append(function)
        value = function()
        if len(calls) == 1:
            if canceled:
                cancel.set()
            raise TimeoutError()
        return value

    def stop():
        cleanup_started.set()
        return cleanup

    bridge.call = call
    bridge.runtime.stop.side_effect = stop
    responses = []
    worker = threading.Thread(target=lambda: responses.append(ops.execute(
        'dispatch-timeout', 'runtime_start', {'mode': 'mapping'}, cancel)))
    worker.start()
    assert cleanup_started.wait(2)
    assert worker.is_alive() and responses == []
    cleanup.set_result({'state': 'STOPPED'})
    worker.join(2)
    assert not worker.is_alive()
    assert responses[0]['code'] == ('canceled' if canceled else 'result_unknown')


@pytest.mark.parametrize('started', [False, True])
def test_dispatch_timeout_distinguishes_queued_from_running_owner(operations, started):
    """Fence pending work, but clean up even before a running callback's first line."""
    ops, bridge, _ = operations
    calls = []
    cleanup = Future()
    cleanup.set_result({'state': 'STOPPED'})
    bridge.runtime.stop.return_value = cleanup

    def call(function, **_kwargs):
        calls.append(function)
        if len(calls) == 1:
            raise OwnerCallTimeout(started)
        return function()

    bridge.call = call
    result = ops.execute('owner-timeout', 'runtime_start', {'mode': 'mapping'})
    assert result['code'] == ('result_unknown' if started else 'dispatch_timeout')
    assert bridge.runtime.stop.call_count == int(started)


def test_running_dispatch_finishes_before_ordered_cleanup_and_terminal_result(operations):
    """A callback exceeding three seconds cannot outlive its queued shutdown."""
    ops, bridge, _ = operations
    release, cleanup_queued, cleanup_started = (threading.Event() for _ in range(3))
    wake, finished = threading.Event(), threading.Event()
    bridge.commands = queue.Queue()
    bridge.guard = SimpleNamespace(trigger=wake.set)
    cleanup = Future()
    calls, responses = [], []

    def call(function, timeout=3.0):
        calls.append(timeout)
        if len(calls) > 1:
            cleanup_queued.set()
        return RosBridge.call(bridge, function, timeout=timeout)

    def start(_payload):
        assert release.wait(5)

    def stop():
        cleanup_started.set()
        return cleanup

    def drain():
        while not finished.is_set():
            wake.wait(5)
            wake.clear()
            RosBridge._drain(bridge)

    bridge.call = call
    bridge._start_runtime.side_effect = start
    bridge.runtime.stop.side_effect = stop
    owner = threading.Thread(target=drain)
    worker = threading.Thread(target=lambda: responses.append(ops.execute(
        'slow-owner', 'runtime_start', {'mode': 'mapping'})))
    owner.start()
    worker.start()
    try:
        assert cleanup_queued.wait(5)
        assert calls == [3.0, 65.0]
        assert worker.is_alive() and responses == []
        release.set()
        assert cleanup_started.wait(2)
        assert worker.is_alive() and responses == []
        cleanup.set_result({'state': 'STOPPED'})
        worker.join(2)
        assert not worker.is_alive()
        assert responses[0]['code'] == 'result_unknown'
    finally:
        release.set()
        if not cleanup.done():
            cleanup.set_result({'state': 'STOPPED'})
        worker.join(2)
        finished.set()
        wake.set()
        owner.join(2)


def test_runtime_shutdown_ignores_resident_queries_but_requires_fall_confirmation(operations):
    """Status/runtime-stop themselves must not become robot-shutdown conflicts."""
    ops, bridge, _ = operations
    bridge.data.system = {'active_background_missions': [
        {'mission_id': 'status', 'capability_id': 'device_operation'},
        {'mission_id': 'weather', 'capability_id': 'get_weather'}],
        'active_foreground_missions': [
        {'mission_id': 'fall', 'capability_id': 'fall_confirmation'}]}
    result = ops.execute('stop-resident', 'runtime_stop', {})
    assert result['code'] == 'preemption_confirmation_required'
    assert result['result']['conflicting_mission_ids'] == ['fall']


def test_resident_restart_preserves_nonzero_movement_epoch(operations):
    """A fresh child does not reset its surviving Manager's movement approval."""
    ops, bridge, _ = operations
    bridge.resident_manager_namespace = '/malbut/resident_manager_test'
    bridge.data.runtime.update(state='STOPPED', ready=False)
    bridge.data.system = {'movement_runtime_id': 'manager', 'movement_epoch': 7}

    def start(_):
        bridge.data.runtime.update(state='RUNNING', mode='mapping', ready=True,
                                   localization={'mode': 'LOCALIZATION'})
    bridge._start_runtime.side_effect = start
    result = ops.execute('restart-resident', 'runtime_start', {
        'mode': 'mapping', 'movement_runtime_id': 'manager', 'movement_epoch': 7})
    assert result['success']
    assert result['result']['preparation_movement_binding'] == {
        'runtime_id': 'manager', 'epoch': 7}


@pytest.mark.parametrize('canceled', [False, True])
def test_resident_preparation_failure_never_waits_on_its_own_manager_stop(
        operations, canceled):
    """Manager owns localization stop; a recursive StopMovement waits on this Action."""
    ops, bridge, _ = operations
    bridge.resident_manager_namespace = '/malbut/resident_manager_test'
    bridge.data.runtime.update(state='RUNNING', ready=True)
    bridge.stop_movement = Mock()
    cancel = threading.Event()

    def fail(_payload):
        if canceled:
            cancel.set()
        raise TimeoutError()
    bridge._start_runtime.side_effect = fail
    result = ops.execute('failed-resident', 'runtime_start', {
        'mode': 'mapping', 'movement_runtime_id': 'manager', 'movement_epoch': 0}, cancel)
    assert result['code'] == ('canceled' if canceled else 'result_unknown')
    bridge.stop_movement.call_async.assert_not_called()
    bridge.runtime.stop.assert_not_called()
