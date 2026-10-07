"""Exercise cross-client stop, admission races and unresolved downstream work."""

from threading import Event, Thread
import json
from types import SimpleNamespace

from action_msgs.msg import GoalStatus
import pytest
import rclpy
from rclpy.action import ActionClient, ActionServer
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile

from malbut_interfaces.action import ConfirmSituation, ExecuteMission, GetWeather
from malbut_interfaces.msg import SystemState
from malbut_interfaces.srv import PrepareLocalization, StopMovement
from malbut_system_manager.localization import LocalizationController
from malbut_system_manager.system_manager_node import SystemManagerNode
from malbut_system_manager.models import MissionCompletion, TerminalOutcome
from test_action_integration import _FollowPersonServer, _wait_future, _wait_until


class _DelayedAdmissionManager(SystemManagerNode):
    """Delay accepted-handle delivery after the public Goal was accepted."""

    def __init__(self):
        self.release_admission = Event()
        super().__init__()

    def _accepted(self, handle):
        self.release_admission.wait(5.0)
        super()._accepted(handle)


@pytest.fixture
def runtime(request):
    """Keep independent action and stop clients on a real ROS executor."""
    options = getattr(request, 'param', {})
    rclpy.init()
    server = _FollowPersonServer(cancel_delay_s=options.get('cancel_delay', 0.0))
    manager_type = _DelayedAdmissionManager if options.get('late_admission') else SystemManagerNode
    manager = manager_type()
    manager._stop_timeout_s = options.get('stop_timeout', 2.0)
    manager._executor_bridge._cancel_completion_timeout_s = options.get('cancel_timeout', 5.0)
    client_node = Node('test_global_stop_client')
    nodes = (server, manager, client_node)
    executor = MultiThreadedExecutor(num_threads=8)
    for node in nodes:
        executor.add_node(node)
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    actions = ActionClient(client_node, ExecuteMission, '/malbut/mission/execute')
    stop = client_node.create_client(StopMovement, '/malbut/mission/stop_movement')
    assert actions.wait_for_server(timeout_sec=5.0)
    assert stop.wait_for_service(timeout_sec=5.0)
    try:
        yield server, manager, actions, stop
    finally:
        if isinstance(manager, _DelayedAdmissionManager):
            manager.release_admission.set()
        manager.begin_shutdown()
        try:
            _wait_until(lambda: manager.downstream_execution_count == 0)
        except AssertionError:
            manager.force_shutdown()
        actions.destroy()
        executor.shutdown(timeout_sec=8.0)
        thread.join(timeout=8.0)
        for node in reversed(nodes):
            executor.remove_node(node)
            node.destroy_node()
        rclpy.shutdown()


def _follow(client):
    goal = ExecuteMission.Goal(
        capability_id='follow_person', arguments_yaml='{target_person_id: first}')
    return _wait_future(client.send_goal_async(goal))


def test_stop_from_another_client_finishes_original_goal_and_is_idempotent(runtime):
    """A retried old stop receipt must not cancel a subsequently started task."""
    server, manager, actions, stop = runtime
    first = _follow(actions)
    _wait_until(server.started.is_set)
    result = first.get_result_async()
    request = StopMovement.Request(request_id='stop-one')
    stopped = _wait_future(stop.call_async(request))
    assert stopped.stopped and stopped.code == 'stopped'
    assert stopped.affected_mission_ids == [bytes(first.goal_id.uuid).hex()]
    completed = _wait_future(result)
    assert completed.status == GoalStatus.STATUS_ABORTED
    assert completed.result.message == 'movement_stopped'
    _wait_until(lambda: not manager._state.movement_stopping)
    second = _follow(actions)
    assert second.accepted
    _wait_until(lambda: len(server.requests) == 2)
    retried = _wait_future(stop.call_async(request))
    assert retried.stopped and retried.affected_mission_ids == stopped.affected_mission_ids
    assert not second.get_result_async().done()
    _wait_future(stop.call_async(StopMovement.Request(request_id='stop-two')))


def test_repeated_async_stops_keep_ros_executor_alive(runtime):
    """Completing stop waiters never destroys a timer already scheduled by ROS."""
    server, _, actions, stop = runtime
    for index in range(12):
        handle = _follow(actions)
        _wait_until(lambda: len(server.requests) == index + 1)
        result = handle.get_result_async()
        stopped = _wait_future(stop.call_async(
            StopMovement.Request(request_id=f'repeated-stop-{index}')))
        assert stopped.stopped
        assert _wait_future(result).status == GoalStatus.STATUS_ABORTED


def test_conditional_stop_does_not_cancel_an_unconfirmed_task(runtime):
    """A stale confirmation cannot cancel an unrelated new mission."""
    server, manager, actions, stop = runtime
    original_epoch = manager._movement_epoch
    handle = _follow(actions)
    _wait_until(server.started.is_set)
    denied = _wait_future(stop.call_async(StopMovement.Request(
        request_id='denied', require_preemption_confirmation=True,
        confirmed_preemption_mission_ids=['old-task'])))
    identity = bytes(handle.goal_id.uuid).hex()
    assert not denied.stopped and denied.affected_mission_ids == []
    assert denied.code == 'preemption_confirmation_required'
    assert denied.unresolved_mission_ids == [identity]
    assert manager._movement_epoch == original_epoch
    assert not handle.get_result_async().done()
    allowed = _wait_future(stop.call_async(StopMovement.Request(
        request_id='confirmed', require_preemption_confirmation=True,
        confirmed_preemption_mission_ids=[identity])))
    assert allowed.stopped
    assert manager._movement_epoch == original_epoch + 1


@pytest.mark.parametrize('runtime', [{'late_admission': True, 'stop_timeout': 0.05}],
                         indirect=True)
def test_stop_covers_accepted_goal_before_scheduler_admission(runtime):
    """A delayed public Goal never starts after the stop has fenced it."""
    server, manager, actions, stop = runtime
    handle = _follow(actions)
    stopped = _wait_future(stop.call_async(StopMovement.Request(request_id='late')))
    assert not stopped.stopped and stopped.unresolved_mission_ids
    assert manager._state.movement_stopping
    manager.release_admission.set()
    result = _wait_future(handle.get_result_async())
    assert result.status == GoalStatus.STATUS_ABORTED
    assert result.result.message == 'movement_stopped'
    assert server.requests == []
    _wait_until(lambda: not manager._state.movement_stopping)


@pytest.mark.parametrize('runtime', [{'cancel_delay': 0.3, 'stop_timeout': 0.05}],
                         indirect=True)
def test_stop_timeout_retains_gate_until_downstream_terminal(runtime):
    """Cancel receipt or public failure cannot establish physical termination."""
    server, manager, actions, stop = runtime
    handle = _follow(actions)
    _wait_until(server.started.is_set)
    result = _wait_future(stop.call_async(StopMovement.Request(request_id='slow')))
    assert not result.stopped and result.code == 'stop_unconfirmed'
    assert manager._state.movement_stopping
    assert not _follow(actions).accepted
    _wait_future(handle.get_result_async())
    _wait_until(lambda: not manager._state.movement_stopping)


def test_confirmed_runtime_shutdown_keeps_all_admission_closed(runtime):
    """Finishing movement does not reopen a runtime that is shutting down."""
    _, manager, actions, stop = runtime
    response = _wait_future(stop.call_async(StopMovement.Request(
        request_id='runtime', shutdown_runtime=True, require_preemption_confirmation=True)))
    assert response.stopped and not manager._accepting_goals
    weather = ExecuteMission.Goal(capability_id='get_weather', arguments_yaml='{}')
    assert not _wait_future(actions.send_goal_async(weather)).accepted


@pytest.mark.parametrize('runtime', [{'cancel_delay': 0.3, 'stop_timeout': 0.05,
                                     'cancel_timeout': 0.05}], indirect=True)
def test_recent_result_separates_public_failure_from_actual_termination(runtime):
    """Late physical completion updates an earlier unresolved public failure."""
    server, manager, actions, stop = runtime
    handle = _follow(actions)
    _wait_until(server.started.is_set)
    identity = bytes(handle.goal_id.uuid).hex()
    _wait_future(stop.call_async(StopMovement.Request(request_id='history')))
    _wait_future(handle.get_result_async())
    assert manager._recent_results[identity]['state'] == 'ABORTED'
    assert not manager._recent_results[identity]['downstream_terminal']
    _wait_until(lambda: manager._recent_results[identity]['downstream_terminal'])
    assert manager._recent_results[identity]['message'] == 'movement_stopped'


def test_recent_results_are_bounded_by_entries_and_encoded_bytes(runtime):
    """Large Unicode results cannot exceed the transient result-topic bound."""
    _, manager, _, _ = runtime
    for index in range(30):
        manager._remember_result(MissionCompletion(
            str(index), TerminalOutcome.SUCCEEDED, result_yaml='한' * 5000,
        ), capability_id='follow_person')
    results = list(manager._recent_results.values())
    assert len(results) <= 20
    assert len(json.dumps(results, ensure_ascii=False).encode('utf-8')) <= 60 * 1024
    assert all(item['result_truncated'] for item in results)
    assert results[-1]['mission_id'] == '29'


@pytest.mark.parametrize('runtime_id,transition_id', [('old-controller', 2), ('controller', 1)])
def test_stale_localization_binding_never_dispatches(runtime, runtime_id, transition_id):
    """Coordinates resolved on a prior map cannot reach a current-map executor."""
    server, manager, actions, _ = runtime
    manager._localization = SimpleNamespace(
        runtime_id='controller', transition_id=2, stop_pending=False, close=lambda: None)
    try:
        goal = ExecuteMission.Goal(
            capability_id='follow_person', arguments_yaml='{target_person_id: first}',
            expected_localization_runtime_id=runtime_id,
            expected_localization_transition_id=transition_id)
        handle = _wait_future(actions.send_goal_async(goal))
        result = _wait_future(handle.get_result_async())
        assert result.status == GoalStatus.STATUS_ABORTED
        assert json.loads(result.result.result_yaml) == {
            'code': 'localization_changed', 'runtime_id': 'controller', 'transition_id': 2}
        assert not server.requests
    finally:
        manager._localization = None


def test_matching_localization_binding_dispatches(runtime):
    """A current transition binding permits the requested existing action."""
    server, manager, actions, _ = runtime
    manager._localization = SimpleNamespace(
        runtime_id='controller', transition_id=2, stop_pending=False, close=lambda: None)
    try:
        goal = ExecuteMission.Goal(
            capability_id='follow_person', arguments_yaml='{target_person_id: second}',
            expected_localization_runtime_id='controller',
            expected_localization_transition_id=2)
        handle = _wait_future(actions.send_goal_async(goal))
        result = _wait_future(handle.get_result_async())
        assert result.status == GoalStatus.STATUS_SUCCEEDED
        assert len(server.requests) == 1
    finally:
        manager._localization = None


def test_movement_sent_before_stop_but_received_after_stop_never_dispatches(runtime):
    """A stop fences requests still in transport without relying on Goal arrival order."""
    server, manager, actions, stop = runtime
    original_epoch = manager._movement_epoch
    delayed = ExecuteMission.Goal(
        capability_id='follow_person', arguments_yaml='{target_person_id: second}',
        require_movement_epoch=True, movement_runtime_id=manager._movement_runtime_id,
        movement_epoch=original_epoch)
    request = StopMovement.Request(request_id='transport-fence')
    response = _wait_future(stop.call_async(request))
    assert response.stopped
    assert manager._movement_epoch == original_epoch + 1
    observed = []
    subscription = server.create_subscription(
        SystemState, '/malbut/state', observed.append,
        QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
    try:
        _wait_until(lambda: bool(observed))
        assert observed[-1].movement_runtime_id == manager._movement_runtime_id
        assert observed[-1].movement_epoch == original_epoch + 1
    finally:
        server.destroy_subscription(subscription)
    _wait_future(stop.call_async(request))
    assert manager._movement_epoch == original_epoch + 1
    handle = _wait_future(actions.send_goal_async(delayed))
    result = _wait_future(handle.get_result_async())
    assert result.status == GoalStatus.STATUS_ABORTED
    assert json.loads(result.result.result_yaml)['code'] == 'movement_epoch_changed'
    assert not server.requests
    delayed.movement_epoch = manager._movement_epoch
    current = _wait_future(actions.send_goal_async(delayed))
    assert _wait_future(current.get_result_async()).status == GoalStatus.STATUS_SUCCEEDED
    assert len(server.requests) == 1


def test_movement_from_previous_manager_lifetime_never_dispatches(runtime):
    """Restarting the Manager cannot make a prior zero epoch current again."""
    server, _, actions, _ = runtime
    goal = ExecuteMission.Goal(
        capability_id='follow_person', arguments_yaml='{target_person_id: second}',
        require_movement_epoch=True, movement_runtime_id='previous-runtime', movement_epoch=0)
    handle = _wait_future(actions.send_goal_async(goal))
    result = _wait_future(handle.get_result_async())
    assert result.status == GoalStatus.STATUS_ABORTED
    assert json.loads(result.result.result_yaml)['code'] == 'movement_epoch_changed'
    assert not server.requests


def test_localization_request_delivered_after_stop_is_fenced(runtime, tmp_path):
    """A delayed integrated LoadMap cannot start its internal AUTO after stop."""
    server, manager, _, stop = runtime
    events = []
    controller = LocalizationController(
        manager, manager._server_group,
        slam=SimpleNamespace(alive=True, start=lambda: events.append('slam_start'),
                             stop=lambda: events.append('slam_stop')),
        on_mode=manager._on_localization_mode, on_pose_ready=manager._on_pose_ready,
        can_switch=manager._base_is_free, admission_lock=manager._effects_lock,
        movement_state=lambda: (manager._movement_runtime_id, manager._movement_epoch),
        lifecycle_service='/test/no_lifecycle', map_server_load_service='/test/no_map',
        service_timeout_s=0.1)
    manager._localization = controller
    controller.start('')
    client = server.create_client(PrepareLocalization, '/malbut/localization/prepare')
    try:
        assert client.wait_for_service(timeout_sec=5.0)
        path = tmp_path / 'home.yaml'
        path.write_text('image: home.pgm\n')
        request = PrepareLocalization.Request(
            map_url=str(path), movement_runtime_id=manager._movement_runtime_id,
            movement_epoch=manager._movement_epoch)
        assert _wait_future(stop.call_async(
            StopMovement.Request(request_id='before-map-delivery'))).stopped
        result = _wait_future(client.call_async(request))
        assert not result.success and result.code == 'movement_epoch_changed'
        assert events == ['slam_start']
        assert controller.transition_id == 1
    finally:
        server.destroy_client(client)


@pytest.mark.parametrize('capability,interface,endpoint,arguments', [
    ('get_weather', GetWeather, '/malbut/weather/get', '{}'),
    ('set_weather_location', ExecuteMission, '/malbut/weather/location/set',
     '{arguments_yaml: "location: 서울"}'),
    ('fall_confirmation', ConfirmSituation, '/malbut/agent/confirm_situation',
     '{request_id: fall-one, situation_type: fall, summary: check}'),
])
def test_movement_epoch_does_not_stop_excluded_tasks(
    runtime, capability, interface, endpoint, arguments,
):
    """A common client's stale movement header cannot cancel fall or weather."""
    server, manager, actions, stop = runtime

    def complete(handle):
        handle.succeed()
        return interface.Result()

    action = ActionServer(server, interface, endpoint, execute_callback=complete,
                          callback_group=ReentrantCallbackGroup())
    try:
        goal = ExecuteMission.Goal(
            capability_id=capability, arguments_yaml=arguments, require_movement_epoch=True,
            movement_runtime_id=manager._movement_runtime_id,
            movement_epoch=manager._movement_epoch)
        assert _wait_future(stop.call_async(
            StopMovement.Request(request_id='exclude-' + capability))).stopped
        handle = _wait_future(actions.send_goal_async(goal))
        assert _wait_future(handle.get_result_async()).status == GoalStatus.STATUS_SUCCEEDED
    finally:
        action.destroy()
