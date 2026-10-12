"""Fixed dependencies, map ownership, and idle transitions without a robot."""

import json
import math
from threading import Lock
from types import SimpleNamespace
from unittest.mock import Mock

from geometry_msgs.msg import PoseWithCovarianceStamped, TransformStamped
from malbut_interfaces.msg import MissionStatus, SystemState
from nav2_msgs.srv import LoadMap, ManageLifecycleNodes
import pytest
from std_srvs.srv import Trigger
from lifecycle_msgs.msg import State

from malbut_bringup.runtime_control import RuntimeControl, required_groups
from malbut_bringup.nav2_stack import MOTION_NODES, PLANNING_NODES
from malbut_bringup import runtime_control


def test_shutdown_joins_callbacks_before_destroying_ros_entities(monkeypatch):
    events = []
    node = Mock()
    node.destroy_node.side_effect = lambda: events.append('destroy node')
    executor = Mock()
    executor.shutdown.side_effect = lambda: events.append('stop executor')
    executor._executor.shutdown.side_effect = lambda **_: events.append('join workers')
    monkeypatch.setattr(runtime_control, 'RuntimeControl', lambda: node)
    monkeypatch.setattr(runtime_control, 'MultiThreadedExecutor', lambda **_: executor)
    monkeypatch.setattr(runtime_control.signal, 'signal', Mock())
    monkeypatch.setattr(runtime_control.rclpy, 'init', Mock())
    monkeypatch.setattr(runtime_control.rclpy, 'ok', lambda: False)
    runtime_control.main()
    assert events == ['stop executor', 'join workers', 'destroy node']
    executor._executor.shutdown.assert_called_once_with(wait=True)


@pytest.mark.parametrize('capabilities,expected', [
    ([], set()),
    (['speech', 'fall_confirmation'], set()),
    (['manual_drive'], {'motion'}),
    (['relocalize'], {'motion'}),
    (['fall_approach'], {'motion', 'planning'}),
    (['follow_person'], {'motion', 'planning', 'person_perception'}),
    (['compute_path_to_pose'], {'planning'}),
    (['patrol'], {'motion', 'planning', 'patrol_inputs'}),
    (['autoslam'], set()),
    (['recovery'], set()),
])
def test_fixed_dependencies(capabilities, expected):
    assert required_groups(capabilities) == expected


def test_fall_rules_do_not_keep_motion_awake_between_actual_missions():
    assert required_groups(['fall_confirmation']) == set()
    assert required_groups(['fall_approach', 'manual_drive']) == {'motion', 'planning'}
    assert required_groups(['manual_drive']) == {'motion'}
    assert required_groups([]) == set()


def _runtime():
    node = object.__new__(RuntimeControl)
    node._lock = Lock()
    node.timeout = 1.0
    node._data_lock = Lock()
    node._capabilities = set()
    node._have_state = True
    node._synced_demand = None
    node._preparations = []
    node._demand_changed = Mock()
    node._closing = False
    node._initialized = True
    node._map_initialized = True
    node._uncertain = False
    node._mapping = False
    node._mapping_ready = False
    node._recovery_pending = False
    node._active = {'motion': False, 'planning': False}
    node._processing_active = {
        service: False for services in runtime_control.PROCESSING_GROUPS.values()
        for service in services}
    node._processing_clients = {name: name for name in node._processing_active}
    node._process_group = Mock()
    node._managers = {'motion': 'motion', 'planning': 'planning'}
    node._manager_states = {
        'motion': 'motion state', 'planning': 'planning state',
        'localization': 'localization state',
    }
    node._node_states = {name: (name, None) for name in (*MOTION_NODES, *PLANNING_NODES)}
    node._localization = 'localization'
    node._localization_started = True
    node._localization_active = True
    node._usage = Mock()
    node._pose = PoseWithCovarianceStamped()
    node._pose_sequence = node._cached_pose_sequence = 1
    node._cached_map_odom = TransformStamped()
    node._cached_map_odom.transform.rotation.w = 1.0
    node.map_path = '/maps/selected.yaml'
    node.map_selection_file = ''
    node.default_map = '/maps/default.yaml'
    node._publish_map = Mock()
    node._map_provider = Mock()
    node._wait_map_tf = Mock()
    node._wait_amcl_pose = Mock()
    node._manage = Mock()
    node.get_logger = Mock()
    node.tf = Mock()
    node.tf.lookup_transform.return_value = TransformStamped()
    return node


def _prepare(node, capability):
    operation = node._prepare(capability, Trigger.Request(), Trigger.Response())
    try:
        operation.send(None)
    except StopIteration as finished:
        return finished.value
    node._on_demand_changed()
    with pytest.raises(StopIteration) as finished:
        operation.send(None)
    return finished.value.value


def test_canceled_preparation_does_not_block_another_capability():
    node = _runtime()
    canceled = node._prepare('follow_person', Trigger.Request(), Trigger.Response())
    canceled.send(None)  # No worker/transition lock is held.
    assert not node._preparations[0][3].done()
    node._state(SystemState(active_foreground_missions=[MissionStatus(
        capability_id='manual_drive', state=MissionStatus.RUNNING)]))
    assert _prepare(node, 'manual_drive').success
    node._manage.assert_called_once_with('motion', ManageLifecycleNodes.Request.RESUME)
    assert len(node._preparations) == 1  # The old RPC did not delay this result.
    node._closing = True
    node._resolve_preparations()
    with pytest.raises(StopIteration) as finished:
        canceled.send(None)
    assert not finished.value.value.success


def test_preparation_waits_for_state_and_completed_dependencies():
    node = _runtime()
    operation = node._prepare('manual_drive', Trigger.Request(), Trigger.Response())
    operation.send(None)
    pending = node._preparations[0][3]
    assert not pending.done()
    node._state(SystemState(active_foreground_missions=[MissionStatus(
        capability_id='manual_drive', state=MissionStatus.RUNNING)]))
    node._resolve_preparations()
    assert not pending.done()  # Seeing demand alone is insufficient.
    node._on_demand_changed()
    with pytest.raises(StopIteration) as finished:
        operation.send(None)
    assert finished.value.value.success


def test_missing_demand_preparation_expires_without_transition(monkeypatch):
    node = _runtime()
    now = [0.0]
    monkeypatch.setattr(runtime_control.time, 'monotonic', lambda: now[0])
    operation = node._prepare('follow_person', Trigger.Request(), Trigger.Response())
    operation.send(None)
    assert not node._preparations[0][3].done()
    now[0] = 2.0
    node._resolve_preparations()
    with pytest.raises(StopIteration) as finished:
        operation.send(None)
    assert not finished.value.value.success
    node._manage.assert_not_called()


def test_repeated_manager_state_only_notifies_when_demand_changes():
    node = _runtime()
    node._have_state = False
    node._state(SystemState())
    node._state(SystemState())
    assert node._demand_changed.trigger.call_count == 1
    running = SystemState(active_foreground_missions=[MissionStatus(
        mission_id='first', capability_id='follow_person', state=MissionStatus.RUNNING)])
    node._state(running)
    node._state(running)
    # Another owner of the same feature and CANCELING do not change demand.
    running.active_background_missions = [MissionStatus(
        mission_id='second', capability_id='follow_person', state=MissionStatus.RUNNING)]
    running.active_foreground_missions[0].state = MissionStatus.CANCELING
    node._state(running)
    running.active_foreground_missions = []
    node._state(running)
    assert node._demand_changed.trigger.call_count == 2
    node._state(SystemState())
    assert node._demand_changed.trigger.call_count == 3


def test_periodic_tick_keeps_tf_refresh_without_dependency_calculation(monkeypatch):
    node = _runtime()
    node._refresh_map_odom = Mock()
    calculate = Mock(wraps=required_groups)
    monkeypatch.setattr(runtime_control, 'required_groups', calculate)
    node._capabilities = {'manual_drive'}
    node._on_demand_changed()
    for _ in range(6):
        node._tick()
        assert _prepare(node, 'manual_drive').success
    assert calculate.call_count == 1
    assert node._refresh_map_odom.call_count == 6
    assert node._manage.call_count == 1


def test_initialization_reconciles_state_received_before_it_is_ready():
    node = _runtime()
    node._initialized = False
    node._node_states = {}
    node._refresh_map_odom = Mock()
    node._state(SystemState(active_foreground_missions=[
        MissionStatus(capability_id='manual_drive')]))
    node._on_demand_changed()
    node._manage.assert_not_called()
    node._tick()
    assert node._initialized and node._active['motion']


def test_changed_demand_during_transition_is_not_lost():
    node = _runtime()
    node._capabilities = {'manual_drive'}
    node._manage.side_effect = lambda *_: node._state(SystemState())
    node._on_demand_changed()
    assert node._active['motion']
    node._manage.side_effect = None
    node._on_demand_changed()
    assert not node._active['motion']
    assert node._manage.call_count == 2


def test_start_and_cancel_coalesce_before_activation(monkeypatch):
    node = _runtime()
    node._sync()
    calculate = Mock(wraps=required_groups)
    monkeypatch.setattr(runtime_control, 'required_groups', calculate)
    node._state(SystemState(active_foreground_missions=[
        MissionStatus(capability_id='follow_person')]))
    node._state(SystemState())
    node._on_demand_changed()
    calculate.assert_not_called()
    node._manage.assert_not_called()


def test_partial_wake_does_not_cache_the_previous_idle_demand():
    node = _runtime()
    node._sync()
    node._capabilities = {'follow_person'}
    node._manage.side_effect = [None, RuntimeError('planner preparation failed')]
    with pytest.raises(RuntimeError, match='preparation failed'):
        node._sync()
    assert node._active['motion']
    node._manage.side_effect = None
    node._capabilities = set()
    node._sync()
    assert not node._active['motion']
    node._manage.assert_called_with('motion', ManageLifecycleNodes.Request.PAUSE)


def test_map_selection_releases_temporary_pose_restore_motion(tmp_path):
    node = _runtime()
    path = tmp_path / 'map.yaml'
    path.write_text('image: home.pgm\n')
    node._sync()

    def restore(*_):
        node._synced_demand = None
        node._active['motion'] = True
    node._map_provider.side_effect = restore
    node._reset_localization = Mock()
    reply = node._load_map(LoadMap.Request(map_url=str(path)), LoadMap.Response())
    assert reply.result == LoadMap.Response.RESULT_SUCCESS
    assert not node._active['motion']
    node._manage.assert_called_once_with('motion', ManageLifecycleNodes.Request.PAUSE)


def test_wake_dependencies_then_pause_dependents_first():
    node = _runtime()
    node._capabilities = {'follow_person'}
    node._sync()
    resume, pause = ManageLifecycleNodes.Request.RESUME, ManageLifecycleNodes.Request.PAUSE
    assert [call.args for call in node._manage.call_args_list] == [
        ('motion', resume), ('planning', resume)]
    node._wait_amcl_pose.assert_not_called()
    node._process_group.assert_any_call('person_perception', True)
    node._manage.reset_mock()
    node._capabilities = set()
    node._sync()
    assert [call.args for call in node._manage.call_args_list] == [
        ('planning', pause), ('motion', pause)]
    assert node._localization_active


def test_shared_planning_stays_active_until_its_last_mission_finishes():
    node = _runtime()
    node._capabilities = {'follow_person', 'compute_path_to_pose'}
    node._sync()
    node._manage.reset_mock()
    node._capabilities = {'compute_path_to_pose'}
    node._sync()
    node._manage.assert_called_once_with('motion', ManageLifecycleNodes.Request.PAUSE)
    assert node._active == {'motion': False, 'planning': True}
    node._manage.reset_mock()
    node._capabilities = set()
    node._sync()
    node._manage.assert_called_once_with('planning', ManageLifecycleNodes.Request.PAUSE)


def test_processing_wake_receivers_first_and_stop_detector_first():
    node = _runtime()
    node._call = Mock(return_value=SimpleNamespace(success=True))
    services = runtime_control.PROCESSING_GROUPS['person_perception']
    RuntimeControl._process_group(node, 'person_perception', True)
    assert [call.args[0] for call in node._call.call_args_list] == list(services)
    assert all(node._processing_active[name] for name in services)
    node._call.reset_mock()
    RuntimeControl._process_group(node, 'person_perception', False)
    assert [call.args[0] for call in node._call.call_args_list] == list(reversed(services))
    assert not any(node._processing_active.values())


def test_partial_processing_wake_is_kept_for_cleanup_after_failure():
    node = _runtime()
    node._call = Mock(side_effect=[
        SimpleNamespace(success=True), RuntimeError('missing identity server')])
    services = runtime_control.PROCESSING_GROUPS['person_perception']
    with pytest.raises(RuntimeError, match='missing'):
        RuntimeControl._process_group(node, 'person_perception', True)
    assert node._processing_active[services[0]]
    assert not any(node._processing_active[name] for name in services[1:])
    node._call = Mock(return_value=SimpleNamespace(success=True))
    RuntimeControl._process_group(node, 'person_perception', False)
    assert not any(node._processing_active.values())


def test_canceling_mission_keeps_dependencies_and_pending_mission_does_not():
    node = _runtime()
    message = SystemState()
    message.active_foreground_missions = [MissionStatus(
        mission_id='old', capability_id='follow_person', state=MissionStatus.CANCELING)]
    message.pending_missions = [MissionStatus(mission_id='new', capability_id='autoslam')]
    node._state(message)
    assert node._capabilities_now() == {'follow_person'}
    node._sync()
    assert all(node._active.values())


def test_idle_and_wake_never_reset_or_reactivate_amcl():
    node = _runtime()
    node._capabilities = {'follow_person'}
    node._sync()
    node._capabilities = set()
    node._sync()
    node._capabilities = {'follow_person'}
    node._sync()
    assert node._localization_active
    assert all(call.args[0] != 'localization' for call in node._manage.call_args_list)
    node._wait_amcl_pose.assert_not_called()


def test_recovery_yields_lifecycle_control_even_after_an_unconfirmed_transition():
    node = _runtime()
    node._uncertain = True
    node._capabilities = {'recovery'}
    node._active = {'motion': True, 'planning': True}
    reply = _prepare(node, 'recovery')
    assert reply.success
    node._manage.assert_not_called()
    assert node._active == {'motion': True, 'planning': True}


def test_finished_recovery_reads_actual_state_before_sleeping_or_waking():
    node = _runtime()
    node._uncertain = True
    message = SystemState(active_foreground_missions=[
        MissionStatus(capability_id='recovery', state=MissionStatus.RUNNING)])
    node._state(message)
    node._state(SystemState())
    node._call = Mock(return_value=SimpleNamespace(
        success=True, current_state=SimpleNamespace(id=State.PRIMARY_STATE_ACTIVE)))
    node._sync()
    assert not node._uncertain and not node._recovery_pending
    assert [call.args for call in node._manage.call_args_list] == [
        ('planning', ManageLifecycleNodes.Request.PAUSE),
        ('motion', ManageLifecycleNodes.Request.PAUSE)]
    assert node._localization_active
    assert node._call.call_count == len(MOTION_NODES) + len(PLANNING_NODES) + 3


def test_incomplete_manual_recovery_never_claims_dependencies_are_ready():
    node = _runtime()
    node._recovery_pending = True
    node._call = Mock(return_value=SimpleNamespace(
        success=True, current_state=SimpleNamespace(id=State.PRIMARY_STATE_UNCONFIGURED)))
    with pytest.raises(RuntimeError, match='inconsistent'):
        node._sync()
    assert node._uncertain
    node._manage.assert_not_called()
    node._report_usage()
    assert not json.loads(node._usage.publish.call_args.args[0].data)['ready']


def test_autoslam_has_no_costmap_consumers_until_its_map_is_ready():
    node = _runtime()
    node._capabilities = {'autoslam'}
    node._sync()
    assert node._active == {'motion': False, 'planning': False}
    node._manage.assert_not_called()
    assert node._localization_active
    assert required_groups({'autoslam'}, mapping_ready=True) == {'motion', 'planning'}
    assert required_groups(set(), mapping_ready=True) == {'motion', 'planning'}


def test_relocalize_can_start_without_the_pose_it_will_initialize():
    node = _runtime()
    node._capabilities = {'relocalize'}
    node._pose = None
    node._wait_amcl_pose.side_effect = RuntimeError('no initial estimate')
    node._sync()
    node._wait_amcl_pose.assert_not_called()
    assert node._active == {'motion': True, 'planning': False}
    assert [call.args for call in node._manage.call_args_list] == [
        ('motion', ManageLifecycleNodes.Request.RESUME)]


def test_incomplete_mapping_restore_cannot_admit_new_navigation():
    node = _runtime()
    node._mapping = True
    node._mapping_ready = False
    node._capabilities = {'navigate_to_pose'}
    with pytest.raises(RuntimeError, match='handoff has not finished'):
        node._sync()
    node._manage.assert_not_called()


def test_failed_mapping_ready_does_not_leave_a_consumer_wake_request():
    node = _runtime()
    node._mapping = True
    node._map_initialized = False
    node._sync = Mock(side_effect=RuntimeError('SLAM TF unavailable'))
    response = node._ready_mapping(Trigger.Request(), Trigger.Response())
    assert not response.success
    assert not node._mapping_ready
    assert required_groups({'autoslam'}, node._mapping_ready) == set()
    node._publish_map.assert_called_once_with(False, 'SLAM TF unavailable')
    assert not json.loads(node._usage.publish.call_args.args[0].data)['ready']


def test_idle_keeps_map_geometry_without_waking_navigation():
    node = _runtime()
    node._sync()
    node._manage.assert_not_called()
    assert node._active == {'motion': False, 'planning': False}
    assert node._localization_active


def test_path_preview_wakes_only_planning_and_returns_to_idle():
    node = _runtime()
    node._capabilities = {'compute_path_to_pose'}
    node._sync()
    assert node._active == {'motion': False, 'planning': True}
    assert [call.args for call in node._manage.call_args_list] == [
        ('planning', ManageLifecycleNodes.Request.RESUME)]
    node._capabilities = set()
    node._sync()
    assert node._active == {'motion': False, 'planning': False}
    assert node._manage.call_args.args == ('planning', ManageLifecycleNodes.Request.PAUSE)


def test_mapping_resets_old_map_before_action_starts_slam():
    node = _runtime()
    node._capabilities = {'autoslam'}
    node._active['planning'] = True
    response = node._start_mapping(Trigger.Request(), Trigger.Response())
    assert response.success
    assert [call.args for call in node._manage.call_args_list] == [
        ('planning', ManageLifecycleNodes.Request.PAUSE),
        ('localization', ManageLifecycleNodes.Request.RESET)]
    assert node._mapping and not node._mapping_ready
    assert not hasattr(node, 'slam')
    node._manage.reset_mock()
    node._ready_mapping(Trigger.Request(), Trigger.Response())
    assert 'planning' in required_groups(node._capabilities_now(), node._mapping_ready)


def test_mapping_cannot_switch_under_another_motion_mission():
    node = _runtime()
    node._capabilities = {'follow_person'}
    response = node._start_mapping(Trigger.Request(), Trigger.Response())
    assert not response.success
    node._manage.assert_not_called()


def test_paused_localization_is_resumed_before_reset():
    node = _runtime()
    node._localization_active = False
    node._reset_localization()
    assert [call.args for call in node._manage.call_args_list] == [
        ('localization', ManageLifecycleNodes.Request.RESUME),
        ('localization', ManageLifecycleNodes.Request.RESET)]
    assert not node._localization_started


def test_reset_invalidates_in_flight_tf_work_from_the_previous_map():
    node = _runtime()
    before = node._pose_sequence

    def replaced_during_lookup(_pose):
        # Model a reset while _refresh_map_odom is outside the data lock doing
        # a TF lookup. Its old result must not repopulate the cleared cache.
        node._reset_localization()
        return TransformStamped()

    node._cached_pose_sequence = -1
    node._map_odom_from_pose = replaced_during_lookup
    node._refresh_map_odom()
    assert node._pose_sequence > before
    assert node._pose is None
    assert node._cached_map_odom is None
    assert node._cached_pose_sequence == -1


def test_mapping_restores_selected_map_without_auto_resuming_navigation():
    node = _runtime()
    node._mapping = node._mapping_ready = True
    node._active['planning'] = True
    node._restore_pose = node._pose
    node._map_odom = TransformStamped()
    node._map_odom.transform.rotation.w = 1.0
    response = node._stop_mapping(Trigger.Request(), Trigger.Response())
    assert response.success
    assert node._map_provider.call_args.args[0] == '/maps/selected.yaml'
    assert not node._active['planning']
    assert not node._mapping


def test_restore_uses_continuous_odometry_not_slams_new_origin():
    node = _runtime()
    node._restore_pose = node._pose
    node._map_odom = TransformStamped()
    old = node._map_odom.transform
    old.translation.x = 3.0
    old.rotation.z, old.rotation.w = math.sin(math.pi / 4), math.cos(math.pi / 4)
    current = TransformStamped()
    current.transform.translation.x = 2.0
    current.transform.rotation.w = 1.0
    node.tf.lookup_transform.return_value = current
    pose = node._pose_after_mapping()
    assert pose.pose.pose.position.x == pytest.approx(3.0)
    assert pose.pose.pose.position.y == pytest.approx(2.0)
    assert pose.pose.pose.orientation.z == pytest.approx(math.sin(math.pi / 4))


def test_mapping_handoff_uses_amcl_pose_not_cached_slam_transform():
    node = _runtime()
    node._pose.header.frame_id = 'map'
    node._pose.pose.pose.position.x = 1.2
    node._pose.pose.pose.orientation.w = 1.0
    odom = TransformStamped()
    odom.transform.translation.x = 0.8
    odom.transform.rotation.w = 1.0
    node.tf.lookup_transform.return_value = odom
    result = node._map_odom_from_pose()
    assert result.transform.translation.x == pytest.approx(0.4)
    assert node.tf.lookup_transform.call_args.args[:2] == ('odom', 'base_footprint')


def test_long_idle_handoff_does_not_need_expired_pose_timestamp_tf():
    """Preserved map/odom state outlives TF's finite history buffer."""
    node = _runtime()
    node._capabilities = {'autoslam'}
    node._cached_map_odom.transform.translation.x = 0.4
    node.tf.lookup_transform.side_effect = RuntimeError('old TF was evicted')
    response = node._start_mapping(Trigger.Request(), Trigger.Response())
    assert response.success
    assert node._map_odom.transform.translation.x == pytest.approx(0.4)
    node.tf.lookup_transform.assert_not_called()


def test_invalid_map_and_busy_map_selection_do_not_reset_live_nodes(tmp_path):
    node = _runtime()
    request = LoadMap.Request(map_url=str(tmp_path / 'absent.yaml'))
    reply = node._load_map(request, LoadMap.Response())
    assert reply.result == LoadMap.Response.RESULT_MAP_DOES_NOT_EXIST
    node._capabilities = {'manual_drive'}
    reply = node._load_map(request, LoadMap.Response())
    assert reply.result == LoadMap.Response.RESULT_UNDEFINED_FAILURE
    node._manage.assert_not_called()
    node._publish_map.assert_not_called()
    node._usage.publish.assert_not_called()


def test_ready_without_mapping_does_not_clear_the_valid_map_status():
    node = _runtime()
    response = node._ready_mapping(Trigger.Request(), Trigger.Response())
    assert not response.success
    assert node._map_initialized
    node._publish_map.assert_not_called()


def test_failed_lifecycle_never_reports_prepared():
    node = _runtime()
    node._capabilities = {'manual_drive'}
    node._manage.side_effect = RuntimeError('transition failed')
    response = _prepare(node, 'manual_drive')
    assert not response.success
    assert 'transition failed' in response.message


def test_map_load_failure_does_not_publish_ready():
    node = _runtime()
    node._initial_pose = object()
    node._map_server = object()
    node._call = Mock(return_value=SimpleNamespace(
        result=LoadMap.Response.RESULT_INVALID_MAP_DATA))
    with pytest.raises(RuntimeError, match='map_server rejected'):
        RuntimeControl._map_provider(node, node.map_path)
    assert not node._map_initialized
    node._publish_map.assert_called_once_with(False, 'preparing map provider')
    node._report_usage()
    assert not json.loads(node._usage.publish.call_args.args[0].data)['ready']
    node._capabilities = {'navigate_to_pose'}
    with pytest.raises(RuntimeError, match='map provider unavailable'):
        node._sync()
    node._manage.assert_not_called()


def test_only_successful_map_load_updates_last_selection(tmp_path):
    from malbut_bringup.map_selection import remembered_map

    node = _runtime()
    selected = tmp_path / 'selected.yaml'
    selected.write_text('image: home.pgm\n')
    node.map_selection_file = str(tmp_path / 'last.txt')
    node._relocalize = None
    node._map_server = object()
    node._call = Mock(return_value=LoadMap.Response(result=LoadMap.Response.RESULT_SUCCESS))
    RuntimeControl._map_provider(node, str(selected))
    assert node._map_initialized
    assert remembered_map(node.map_selection_file) == str(selected)

    node._call.return_value = LoadMap.Response(result=LoadMap.Response.RESULT_INVALID_MAP_DATA)
    with pytest.raises(RuntimeError, match='map_server rejected'):
        RuntimeControl._map_provider(node, str(tmp_path / 'failed.yaml'))
    assert remembered_map(node.map_selection_file) == str(selected)


def test_selection_write_failure_does_not_break_loaded_map(monkeypatch):
    node = _runtime()
    node._relocalize = None
    node._map_server = object()
    node._call = Mock(return_value=LoadMap.Response(result=LoadMap.Response.RESULT_SUCCESS))
    monkeypatch.setattr(runtime_control, 'remember_map', Mock(side_effect=OSError('read-only')))
    RuntimeControl._map_provider(node, node.map_path)
    assert node._map_initialized
    node.get_logger().warning.assert_called_once()
    node._publish_map.assert_called_with(True, 'map provider ready')


def test_saved_map_waits_for_late_pose_restore_server(monkeypatch):
    node = _runtime()
    node._map_server = object()
    node._call = Mock(return_value=LoadMap.Response(result=LoadMap.Response.RESULT_SUCCESS))
    finished = SimpleNamespace(done=lambda: True, result=lambda: SimpleNamespace(
        result=SimpleNamespace(success=True, message='pose restored')))
    handle = SimpleNamespace(accepted=True, get_result_async=lambda: finished)
    accepted = SimpleNamespace(done=lambda: True, result=lambda: handle)
    node._relocalize = Mock()
    node._relocalize.server_is_ready.side_effect = [False, False, True]
    node._relocalize.send_goal_async.return_value = accepted
    monkeypatch.setattr(runtime_control.time, 'sleep', Mock())
    RuntimeControl._map_provider(node, node.map_path)
    assert node._relocalize.server_is_ready.call_count == 3
    node._relocalize.send_goal_async.assert_called_once()
    assert node._map_initialized
    node._sync()
    assert not node._active['motion']


def test_missing_pose_restore_server_is_not_silently_reported_ready(monkeypatch):
    node = _runtime()
    node._map_server = object()
    node._call = Mock(return_value=LoadMap.Response(result=LoadMap.Response.RESULT_SUCCESS))
    node._relocalize = Mock()
    node._relocalize.server_is_ready.return_value = False
    now = [0.0]
    monkeypatch.setattr(runtime_control.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(runtime_control.time, 'sleep',
                        lambda seconds: now.__setitem__(0, now[0] + seconds))
    with pytest.raises(RuntimeError, match='pose restore: Action server unavailable'):
        RuntimeControl._map_provider(node, node.map_path)
    assert not node._map_initialized
    assert node._synced_demand is None
    node._relocalize.send_goal_async.assert_not_called()
    assert all(not call.args[0] for call in node._publish_map.call_args_list)
