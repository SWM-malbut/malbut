"""Keep all operations under one Manager across child runtime lifetimes."""

import json
from pathlib import Path
from threading import Event, Thread
import time
from types import SimpleNamespace

from action_msgs.msg import GoalStatus
import pytest
import rclpy
from rclpy.action import ActionClient, ActionServer, CancelResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_msgs.msg import String
import yaml

from malbut_interfaces.action import DeviceOperation, ExecuteMission, GetWeather
from malbut_interfaces.srv import StopMovement
from malbut_system_manager.system_manager_node import SystemManagerNode
from test_action_integration import _FollowPersonServer, _wait_future, _wait_until


@pytest.fixture
def resident():
    """Use real Manager Actions with controllable runtime and feature servers."""
    rclpy.init(args=['--ros-args', '-p', 'resident_runtime:=true'])
    manifests = Path(__file__).resolve().parents[2] / 'malbut_interfaces/capabilities'
    manager = SystemManagerNode(manifest_directory=str(manifests))
    client_node = Node('robot_cloud_sync')
    follower = _FollowPersonServer()
    release_weather = Event()
    operations = []
    weather_calls = []
    feed = {'enabled': False, 'state': 'STOPPED', 'runtime_id': 'first', 'age': 0}
    publisher = client_node.create_publisher(
        String, '/malbut/runtime/state',
        QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL),
    )

    def publish():
        if feed['enabled']:
            publisher.publish(String(data=json.dumps({
                'state': feed['state'], 'runtime_id': feed['runtime_id'],
                'mode': 'mapping', 'map': '',
                'observed_at': time.time() - feed['age'],
                'movement_runtime_id': feed.get('movement_runtime_id', ''),
                'movement_epoch': feed.get('movement_epoch', 0),
            })))

    timer = client_node.create_timer(0.05, publish)

    def weather(handle):
        weather_calls.append(handle)
        deadline = time.monotonic() + 8
        while not release_weather.is_set() and not handle.is_cancel_requested:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        if handle.is_cancel_requested:
            handle.canceled()
        else:
            handle.succeed()
        return GetWeather.Result()

    def device(handle):
        operations.append(handle.request.operation)
        if handle.request.operation == 'runtime_start':
            deadline = time.monotonic() + 8
            while not handle.is_cancel_requested and time.monotonic() < deadline:
                time.sleep(0.01)
            if handle.is_cancel_requested:
                handle.canceled()
                return DeviceOperation.Result(code='canceled', result_json='{}')
            handle.abort()
            return DeviceOperation.Result(code='timeout', result_json='{}')
        handle.succeed()
        return DeviceOperation.Result(success=True, code='completed', result_json='{}')

    servers = [ActionServer(
        client_node, kind, endpoint, callback,
        cancel_callback=lambda _: CancelResponse.ACCEPT,
        callback_group=ReentrantCallbackGroup(),
    ) for kind, endpoint, callback in (
        (GetWeather, '/malbut/weather/get', weather),
        (DeviceOperation, '/malbut/device/operate', device),
    )]
    actions = ActionClient(client_node, ExecuteMission, '/malbut/mission/execute')
    stop = client_node.create_client(StopMovement, '/malbut/mission/stop_movement')
    executor = MultiThreadedExecutor(num_threads=8)
    nodes = (client_node, follower, manager)
    for node in nodes:
        executor.add_node(node)
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    assert actions.wait_for_server(timeout_sec=5)
    assert stop.wait_for_service(timeout_sec=5)
    try:
        yield SimpleNamespace(
            manager=manager, actions=actions, stop=stop, feed=feed,
            operations=operations, follower=follower, weather_calls=weather_calls,
            release_weather=release_weather,
        )
    finally:
        release_weather.set()
        timer.cancel()
        manager.begin_shutdown()
        try:
            _wait_until(lambda: manager.downstream_execution_count == 0)
        except AssertionError:
            manager.force_shutdown()
        executor.shutdown(timeout_sec=8)
        thread.join(8)
        actions.destroy()
        for server in servers:
            server.destroy()
        for node in reversed(nodes):
            node.destroy_node()
        rclpy.shutdown()


def _send(resident, capability, arguments=None, **policy):
    return _wait_future(resident.actions.send_goal_async(ExecuteMission.Goal(
        capability_id=capability, arguments_yaml=yaml.safe_dump(arguments or {}),
        **policy,
    )))


def _running(resident, runtime_id):
    resident.feed.update(
        enabled=True, state='RUNNING', runtime_id=runtime_id, age=0,
        movement_runtime_id=resident.manager._movement_runtime_id,
        movement_epoch=resident.manager._movement_epoch,
    )
    _wait_until(lambda: resident.manager._runtime_id == runtime_id
                and resident.manager._state.ready)


def test_shutdown_keeps_manager_weather_and_queries_then_accepts_new_runtime(resident):
    """A child shutdown cannot cancel its resident Manager or weather request."""
    denied = _send(resident, 'follow_person')
    assert _wait_future(denied.get_result_async()).status == GoalStatus.STATUS_ABORTED
    assert not resident.follower.requests
    weather = _send(resident, 'get_weather')
    _wait_until(lambda: bool(resident.weather_calls))
    _running(resident, 'first')
    movement = _send(resident, 'follow_person', {'target_person_id': 'first'})
    _wait_until(resident.follower.started.is_set)
    stopped = _wait_future(resident.stop.call_async(StopMovement.Request(
        request_id='shutdown-child', shutdown_runtime=True,
        require_preemption_confirmation=True,
        confirmed_preemption_mission_ids=[bytes(movement.goal_id.uuid).hex()],
    )))
    assert stopped.stopped
    assert _wait_future(movement.get_result_async()).status == GoalStatus.STATUS_ABORTED
    assert not weather.get_result_async().done()
    query = _send(resident, 'device_operation', {
        'request_id': 'after-shutdown', 'operation': 'status', 'arguments_json': '{}',
    })
    assert _wait_future(query.get_result_async()).status == GoalStatus.STATUS_SUCCEEDED
    denied = _send(resident, 'follow_person')
    assert _wait_future(denied.get_result_async()).status == GoalStatus.STATUS_ABORTED
    epoch = resident.manager._movement_epoch
    resident.feed['state'] = 'STOPPED'
    _wait_until(lambda: not resident.manager._state.ready)
    _running(resident, 'second')
    assert resident.manager._movement_epoch == epoch
    fresh = _send(resident, 'follow_person', {'target_person_id': 'second'})
    assert _wait_future(fresh.get_result_async()).status == GoalStatus.STATUS_SUCCEEDED
    resident.release_weather.set()
    assert _wait_future(weather.get_result_async()).status == GoalStatus.STATUS_SUCCEEDED


def test_global_stop_cancels_preparation_and_rejects_its_old_epoch(resident):
    """A queued robot start cannot evade stop because it has no BASE resource."""
    manager = resident.manager
    binding = {'movement_runtime_id': manager._movement_runtime_id,
               'movement_epoch': manager._movement_epoch}
    arguments = {
        'request_id': 'prepare-one', 'operation': 'runtime_start',
        'arguments_json': json.dumps({'mode': 'mapping', **binding}),
    }
    preparation = _send(resident, 'device_operation', arguments,
                        require_movement_epoch=True, **binding)
    _wait_until(lambda: resident.operations == ['runtime_start'])
    stopped = _wait_future(resident.stop.call_async(
        StopMovement.Request(request_id='stop-preparation')))
    assert stopped.stopped
    assert bytes(preparation.goal_id.uuid).hex() in stopped.affected_mission_ids
    assert _wait_future(preparation.get_result_async()).status == GoalStatus.STATUS_ABORTED
    arguments['request_id'] = 'late-preparation'
    late = _send(resident, 'device_operation', arguments,
                 require_movement_epoch=True, **binding)
    assert _wait_future(late.get_result_async()).status == GoalStatus.STATUS_ABORTED
    assert resident.operations == ['runtime_start']


def test_stale_runtime_report_stops_motion_without_replaying_it(resident):
    """An old RUNNING report cannot authorize a fresh mission or resume one."""
    _running(resident, 'live')
    movement = _send(resident, 'follow_person', {'target_person_id': 'first'})
    _wait_until(resident.follower.started.is_set)
    resident.feed['age'] = 30
    _wait_until(lambda: not resident.manager._state.ready)
    assert _wait_future(movement.get_result_async()).status == GoalStatus.STATUS_ABORTED
    denied = _send(resident, 'follow_person')
    assert _wait_future(denied.get_result_async()).status == GoalStatus.STATUS_ABORTED
    assert len(resident.follower.requests) == 1


def test_late_child_start_report_cannot_erase_a_stop(resident):
    """A process becoming RUNNING after stop must retain its original binding."""
    manager = resident.manager
    resident.feed.update(
        state='RUNNING', runtime_id='late-child',
        movement_runtime_id=manager._movement_runtime_id,
        movement_epoch=manager._movement_epoch,
    )
    stopped = _wait_future(resident.stop.call_async(
        StopMovement.Request(request_id='stop-before-child-start')))
    assert stopped.stopped
    resident.feed['enabled'] = True
    _wait_until(lambda: manager._runtime_received > 0)
    assert not manager._state.ready and not manager._runtime_id
    denied = _send(resident, 'follow_person')
    assert _wait_future(denied.get_result_async()).status == GoalStatus.STATUS_ABORTED
    assert not resident.follower.requests
