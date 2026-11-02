"""Exercise standalone direction -> real Manager -> fake Nav2, without STT."""

import json
import math
from pathlib import Path
import time
from types import SimpleNamespace

from builtin_interfaces.msg import Duration
from nav2_msgs.action import Spin
from nav_msgs.msg import Odometry
import pytest
import rclpy
from rclpy.action import ActionServer, CancelResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.task import Future
from std_msgs.msg import String

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
def robot(request):
    """Keep motion inside a fake Nav2 server and exercise the real public action."""
    rclpy.init()
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
    runtime = peer.create_publisher(String, '/malbut/runtime/state', 10)

    def publish():
        message = Odometry()
        if not sensor.stale:
            message.header.stamp = peer.get_clock().now().to_msg()
        message.twist.twist.linear.x = sensor.speed
        odom.publish(message)
        runtime.publish(String(data=json.dumps(dict(
            ready=True, active_groups=[], capabilities=[]))))

    # Match transient-local state delivery in Bringup.
    peer.destroy_publisher(runtime)
    from rclpy.qos import DurabilityPolicy, QoSProfile
    runtime = peer.create_publisher(String, '/malbut/runtime/state', QoSProfile(
        depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
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
