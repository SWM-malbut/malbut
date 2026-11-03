"""Exercise standalone direction -> real Manager -> fake Nav2, without STT."""

import json
import math
from pathlib import Path
import time
from types import SimpleNamespace

from action_msgs.msg import GoalInfo, GoalStatus
from action_msgs.srv import CancelGoal
from builtin_interfaces.msg import Duration
from nav2_msgs.action import Spin
from nav_msgs.msg import Odometry
import pytest
import rclpy
from rclpy.action import ActionServer, CancelResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from rclpy.task import Future
from std_msgs.msg import String
from unique_identifier_msgs.msg import UUID

from malbut_system_manager.models import MissionRecord
from malbut_system_manager.sound_direction import Directions
from malbut_system_manager.sound_turn import SoundTurn
from malbut_system_manager.system_manager_node import SystemManagerNode


class Reader(Directions):
    """Inject measured angles, with no microphone, speech recognition or camera."""

    connected = True

    def close(self):
        """Simulate closing the physical serial port."""
        self.connected = False


@pytest.fixture
def robot(request, monkeypatch):
    """Keep motion inside a fake Nav2 server and exercise the real public action."""
    # Other colcon packages also provide /spin and /malbut/state. Keep this
    # simulated robot off their DDS graph and off the physical robot's network.
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    rclpy.init(domain_id=212)
    request.addfinalizer(rclpy.try_shutdown)
    executor = SingleThreadedExecutor()
    peer = Node('sound_turn_test_sensors')
    request.addfinalizer(peer.destroy_node)
    manifests = Path(__file__).resolve().parents[2] / 'malbut_interfaces/capabilities'
    manager = SystemManagerNode(manifest_directory=str(manifests))
    request.addfinalizer(manager.destroy_node)
    reader = Reader()
    turn = SoundTurn(reader=reader)
    request.addfinalizer(turn.destroy_node)
    requests, completions = [], []
    sensor = SimpleNamespace(speed=0., stale=False)
    odom = peer.create_publisher(Odometry, '/odom', 10)
    runtime = peer.create_publisher(String, '/malbut/runtime/state', QoSProfile(
        depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))

    def publish():
        message = Odometry()
        if not sensor.stale:
            message.header.stamp = peer.get_clock().now().to_msg()
        message.twist.twist.linear.x = sensor.speed
        odom.publish(message)
        runtime.publish(String(data=json.dumps(dict(
            ready=True, active_groups=[], capabilities=[]))))

    timer = peer.create_timer(.05, publish)

    async def execute(goal):
        requests.append(goal)
        completion = Future()
        completions.append(completion)
        await completion
        if goal.is_cancel_requested:
            goal.canceled()
        elif completion.result():
            goal.succeed()
        else:
            goal.abort()
        return Spin.Result(total_elapsed_time=Duration(sec=1))

    server = ActionServer(peer, Spin, '/spin', execute_callback=execute,
                          cancel_callback=lambda _: CancelResponse.ACCEPT,
                          callback_group=ReentrantCallbackGroup())
    request.addfinalizer(server.destroy)
    for node in (manager, peer, turn):
        executor.add_node(node)

    def until(predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while not predicate():
            assert time.monotonic() < deadline, 'ROS sound turn condition timed out'
            executor.spin_once(timeout_sec=.01)

    def pump(seconds=.15):
        deadline = time.monotonic() + seconds
        until(lambda: time.monotonic() >= deadline)

    try:
        assert turn.client.wait_for_server(timeout_sec=8)
        mission = MissionRecord('discover', manager._registry.get('spin'), {})
        assert manager._executor_bridge._client_for(mission).wait_for_server(timeout_sec=8)
        until(lambda: turn.state is not None and turn.runtime_idle
              and turn.stationary_since is not None
              and time.monotonic() - turn.stationary_since > .5)
        yield SimpleNamespace(**locals())
    finally:
        turn.close()
        for completion in completions:
            if not completion.done():
                completion.set_result(False)
        manager.begin_shutdown()
        until(lambda: manager.downstream_execution_count == 0 and not turn.inflight)
        turn.client.destroy()
        timer.cancel()
        executor.shutdown(timeout_sec=0)


@pytest.mark.parametrize('raw,yaw', [(240., math.pi / 2), (60., -math.pi / 2), (330., -math.pi)])
def test_angle_rotates_without_any_speech_or_person_nodes(robot, raw, yaw):
    """Left, right and rear directions dispatch exactly one existing Spin."""
    h = robot
    h.reader.add(raw, time.monotonic())
    h.until(lambda: len(h.requests) == 1)
    assert h.requests[0].request.target_yaw == pytest.approx(yaw)
    assert h.requests[0].request.time_allowance.sec == 20
    assert set(h.peer.get_node_names()) == {
        'system_manager', 'sound_turn', 'sound_turn_test_sensors'}
    h.completions[0].set_result(True)
    h.until(lambda: not h.turn.inflight)
    h.pump()
    assert len(h.requests) == 1
    assert h.manager.downstream_execution_count == 0


@pytest.mark.parametrize('reason', ['forward', 'old', 'future', 'moving', 'stale_odom',
                                    'ambiguous', 'disconnected', 'cooldown'])
def test_unsuitable_measurement_does_not_dispatch(robot, reason):
    """Discard invalid inputs instead of queuing a delayed rotation."""
    h = robot
    if reason in ('moving', 'stale_odom'):
        h.sensor.speed = .1 if reason == 'moving' else 0.
        h.sensor.stale = reason == 'stale_odom'
        h.pump()
    if reason == 'disconnected':
        h.reader.connected = False
    if reason == 'cooldown':
        h.turn.next_turn = time.monotonic() + 3
    at = time.monotonic() + (-2 if reason == 'old' else 2 if reason == 'future' else 0)
    h.reader.add(150. if reason == 'forward' else 240., at)
    if reason == 'ambiguous':
        h.reader.add(60., at)
    h.pump()
    assert not h.requests and not h.turn.inflight
    assert h.reader.take(time.monotonic()) is None


def test_busy_rotation_drops_new_angles_and_failure_does_not_start_tracking(robot):
    """A single flight ends on failure without any fallback mission."""
    h = robot
    h.reader.add(240., time.monotonic())
    h.until(lambda: len(h.requests) == 1)
    h.reader.add(60., time.monotonic())
    h.pump()
    assert h.reader.take(time.monotonic()) is None
    h.completions[0].set_result(False)
    h.until(lambda: not h.turn.inflight)
    h.turn.next_turn = 0.
    h.pump(.7)
    assert len(h.requests) == 1 and not list(h.manager._state.all())


@pytest.mark.parametrize('reason', ['disconnect', 'shutdown', 'stale_odom', 'deadline'])
def test_owned_turn_cancels_and_waits_for_downstream_stop(robot, reason):
    """Losing a prerequisite requests cancellation and blocks another turn."""
    h = robot
    h.reader.add(240., time.monotonic())
    h.until(lambda: len(h.requests) == 1)
    if reason == 'disconnect':
        h.reader.connected = False
    elif reason == 'shutdown':
        h.turn.close()
    elif reason == 'deadline':
        h.turn.deadline = time.monotonic() - 1
    else:
        h.sensor.stale = True
    h.until(lambda: h.requests[0].is_cancel_requested)
    assert h.turn.inflight
    h.reader.add(60., time.monotonic())
    h.pump()
    assert len(h.requests) == 1
    h.completions[0].set_result(False)
    h.until(lambda: not h.turn.inflight)


def test_manager_busy_admission_does_not_preempt_existing_work(robot):
    """Atomic idle-only admission rejects a race after an earlier idle report."""
    h = robot
    h.manager._state.recharging = True
    h.reader.add(240., time.monotonic())
    h.until(lambda: h.turn.next_turn > 0)
    assert not h.requests and not h.turn.inflight


def owned_handle(h, *, result_failure='', cancel_failure=''):
    """Inject failures at the public Action transport without moving a robot."""
    now = SimpleNamespace(value=time.monotonic())
    h.turn.clock = lambda: now.value
    h.turn.inflight = True
    h.turn.mission_id = '12' * 16
    h.turn.deadline, h.turn.accept_deadline = now.value + 55, now.value + 5
    result = Future()
    calls = SimpleNamespace(result=0, cancel=0, cancel_futures=[])

    def get_result():
        calls.result += 1
        if calls.result == 1 and result_failure:
            if result_failure == 'request':
                raise RuntimeError('result request failed')
            failed = Future()
            if result_failure == 'future':
                failed.set_exception(RuntimeError('result response failed'))
            else:
                failed.set_result(SimpleNamespace(status=GoalStatus.STATUS_UNKNOWN))
            return failed
        return result

    def cancel():
        calls.cancel += 1
        response = Future()
        calls.cancel_futures.append(response)
        if calls.cancel == 1 and cancel_failure:
            if cancel_failure == 'request':
                raise RuntimeError('cancel request failed')
            if cancel_failure == 'pending':
                return response
            if cancel_failure == 'future':
                response.set_exception(RuntimeError('cancel response failed'))
            else:
                response.set_result(CancelGoal.Response(
                    return_code=CancelGoal.Response.ERROR_REJECTED))
        else:
            response.set_result(CancelGoal.Response(goals_canceling=[GoalInfo(
                goal_id=UUID(uuid=list(bytes.fromhex(h.turn.mission_id))))]))
        return response

    handle = SimpleNamespace(accepted=True, get_result_async=get_result, cancel_goal_async=cancel)
    h.turn._accepted(SimpleNamespace(result=lambda: handle))
    return now, calls, result


@pytest.mark.parametrize('failure', ['request', 'future', 'unknown'])
def test_unconfirmed_result_cancels_and_keeps_ownership_until_terminal(robot, failure):
    """Lost/unknown results never release an owned goal or disable cancellation."""
    h = robot
    now, calls, result = owned_handle(h, result_failure=failure)
    assert h.turn.inflight and h.turn.cancel_requested and calls.cancel == 1
    for _ in range(20):
        h.turn.tick()
    assert calls.result == 1 and calls.cancel == 1
    now.value += 1.01
    h.reader.add(60., now.value)
    h.turn.tick()
    assert calls.result == 2 and calls.cancel == 1
    assert h.turn.inflight and not h.requests
    result.set_result(SimpleNamespace(status=GoalStatus.STATUS_CANCELED))
    assert not h.turn.inflight


@pytest.mark.parametrize('failure', [
    'request', 'future', 'rejected', 'pending', 'shutdown_request',
])
def test_unconfirmed_cancel_retries_with_backoff_without_releasing_ownership(robot, failure):
    """A failed cancel remains desired, including during graceful shutdown."""
    h = robot
    closing = failure == 'shutdown_request'
    now, calls, result = owned_handle(h, cancel_failure='request' if closing else failure)
    h.turn.close() if closing else h.turn.cancel()
    for _ in range(20):
        h.turn.cancel()
    assert h.turn.inflight and calls.cancel == 1
    now.value += 1.01
    h.until(lambda: calls.cancel == 2)
    assert h.turn.inflight and h.turn.cancel_confirmed
    if failure == 'pending':
        assert calls.cancel_futures[0].cancelled()
        h.turn._canceled(calls.cancel_futures[0])
        assert h.turn.cancel_confirmed
    now.value += 2.
    h.turn.tick()
    assert calls.cancel == 2 and not h.requests
    result.set_result(SimpleNamespace(status=GoalStatus.STATUS_CANCELED))
    assert not h.turn.inflight


def test_cancel_timeout_unregisters_old_rpc_and_keeps_waiting_for_the_result(robot):
    """Retire a stalled RPC, ignoring its late reply while retaining goal ownership."""
    h = robot
    h.reader.add(240., time.monotonic())
    h.until(lambda: len(h.requests) == 1 and h.turn.goal_handle is not None)
    now = SimpleNamespace(value=time.monotonic())
    h.turn.clock = lambda: now.value
    result = h.turn.result_future
    h.turn.cancel()
    pending = h.turn.cancel_future
    assert pending in h.turn.client._pending_cancel_requests.values()
    # Do not spin the server until the first response has missed its deadline.
    now.value += 1.01
    h.turn.cancel()
    assert pending.cancelled() and h.turn.cancel_future is not pending
    h.until(lambda: pending not in h.turn.client._pending_cancel_requests.values()
            and h.requests[0].is_cancel_requested)
    assert h.turn.inflight and h.turn.result_future is result and len(h.requests) == 1
    h.completions[0].set_result(False)
    h.until(lambda: not h.turn.inflight)


def test_old_odometry_does_not_gain_freshness_at_reception(robot, monkeypatch):
    """Source age still limits authorization after a delayed message arrives."""
    h = robot
    message = Odometry()
    message.header.stamp = h.peer.get_clock().now().to_msg()
    stamp = message.header.stamp.sec * 10**9 + message.header.stamp.nanosec
    ros_time = SimpleNamespace(nanoseconds=stamp + 1_400_000_000)
    monkeypatch.setattr(h.turn, 'get_clock', lambda: SimpleNamespace(now=lambda: ros_time))
    h.turn._odom(message)
    assert h.turn._available(time.monotonic())
    ros_time.nanoseconds += 200_000_000
    h.reader.add(240., time.monotonic())
    h.turn.tick()
    assert not h.turn.inflight and not h.requests


def test_replayed_odometry_cannot_confirm_stationarity(robot):
    """Repeating the same timestamp cannot renew a stationary observation."""
    h = robot
    message = Odometry()
    message.header.stamp.sec, message.header.stamp.nanosec = divmod(h.turn.odom_stamp, 10**9)
    h.turn._odom(message)
    assert h.turn.stationary_since is None
    assert not h.turn._available(time.monotonic())
