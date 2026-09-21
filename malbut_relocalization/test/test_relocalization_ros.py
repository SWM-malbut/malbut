"""Find the pose against stand-in AMCL, Spin, map and LiDAR over DDS."""

import json
import math
import os
import time

from geometry_msgs.msg import PoseWithCovarianceStamped, TransformStamped
from lifecycle_msgs.msg import State
from lifecycle_msgs.srv import GetState
from malbut_interfaces.action import Relocalize
from malbut_relocalization.pose_store import map_identity, write_pose
from malbut_relocalization.relocalization_node import pose_message, Relocalization
from nav2_msgs.action import Spin
from nav_msgs.msg import OccupancyGrid
import pytest
import rclpy
from rclpy.action import ActionClient, ActionServer
from rclpy.executors import MultiThreadedExecutor
from rclpy.parameter import Parameter
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import LaserScan
from std_msgs.msg import String
from std_srvs.srv import Empty
from tf2_ros import StaticTransformBroadcaster

TRUE_POSE = (0.8, 0.4, 0.3)


@pytest.fixture
def graph(room):
    """Run the server beside stand-in AMCL, behavior server and manager."""
    context = rclpy.Context()
    rclpy.init(context=context, domain_id=130 + os.getpid() % 30)
    server = Relocalization(context=context, parameter_overrides=[
        Parameter('pose_file', value=str(room.map_file.parent / 'last_pose.yaml')),
        Parameter('timeout_s', value=5.0),
    ])
    amcl = rclpy.create_node('amcl', context=context)
    calls = []
    latched = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.TRANSIENT_LOCAL)
    estimates = amcl.create_publisher(PoseWithCovarianceStamped, '/amcl_pose', 10)

    def publish(x, y, yaw):
        message = pose_message({'x': x, 'y': y, 'yaw': yaw, 'covariance': [0.01] * 36},
                               amcl.get_clock().now().to_msg())
        estimates.publish(message)

    def initial_pose(message):
        calls.append('initialpose')
        position = message.pose.pose.position
        publish(position.x, position.y,
                2 * math.atan2(message.pose.pose.orientation.z, message.pose.pose.orientation.w))

    def service(name, action=None):
        def handle(_request, response):
            calls.append(name)
            if action:
                action()
            return response
        return handle

    amcl.create_subscription(PoseWithCovarianceStamped, '/initialpose', initial_pose, 10)
    amcl.create_service(GetState, '/amcl/get_state', lambda _, response: (
        setattr(response.current_state, 'id', State.PRIMARY_STATE_ACTIVE), response)[1])
    amcl.create_service(Empty, '/reinitialize_global_localization', service('global'))
    # After the spin, AMCL converges on the robot's actual pose.
    amcl.create_service(Empty, '/request_nomotion_update',
                        service('nomotion', lambda: publish(*TRUE_POSE)))
    map_publisher = amcl.create_publisher(OccupancyGrid, '/map', latched)
    scans = amcl.create_publisher(LaserScan, '/scan_raw', 10)

    def scan():
        message = room.scan(*TRUE_POSE)
        message.header.stamp = amcl.get_clock().now().to_msg()
        scans.publish(message)

    amcl.create_timer(0.1, scan)
    laser = TransformStamped()
    laser.header.frame_id, laser.child_frame_id = 'base_footprint', 'laser'
    laser.transform.rotation.w = 1.0
    static = StaticTransformBroadcaster(amcl)
    static.sendTransform(laser)
    ActionServer(amcl, Spin, '/spin', lambda handle: (
        calls.append('spin'), handle.succeed(), Spin.Result())[2])
    manager = rclpy.create_node('relocalization_test_manager', context=context)
    state = manager.create_publisher(String, '/malbut/localization/state', latched)
    client = ActionClient(manager, Relocalize, '/relocalize')
    executor = MultiThreadedExecutor(num_threads=6, context=context)
    for node in (server, amcl, manager):
        executor.add_node(node)

    def select():
        state.publish(String(data=json.dumps({
            'mode': 'LOCALIZATION', 'map': str(room.map_file)})))
        map_publisher.publish(room.grid())

    yield executor, select, client, calls
    executor.shutdown()
    for node in (server, amcl, manager):
        node.destroy_node()
    rclpy.shutdown(context=context)


def _run(executor, client, method=Relocalize.Goal.AUTO):
    assert client.wait_for_server(timeout_sec=5.0) or pytest.fail('no /relocalize')
    goal = Relocalize.Goal()
    goal.method = method
    sent = client.send_goal_async(goal)
    deadline = time.monotonic() + 30.0
    while not sent.done() and time.monotonic() < deadline:
        executor.spin_once(timeout_sec=0.05)
    result = sent.result().get_result_async()
    while not result.done() and time.monotonic() < deadline:
        executor.spin_once(timeout_sec=0.05)
    return result.result().result


def _saved(room, x, y, yaw):
    covariance = [0.05 if index % 7 == 0 else 0.0 for index in range(36)]
    write_pose(room.map_file.parent / 'last_pose.yaml', map_identity(room.map_file),
               {'frame_id': 'map', 'x': x, 'y': y, 'yaw': yaw, 'covariance': covariance})


def test_saved_pose_is_confirmed_by_the_scan(graph, room):
    """The robot stayed put: its saved pose reaches AMCL and matches the map."""
    executor, select, client, calls = graph
    _saved(room, *TRUE_POSE)
    result = _run(executor, client)
    assert not result.success and 'no saved map is selected' in result.message
    select()
    result = _run(executor, client)
    assert result.success, result.message
    assert 'saved pose confirmed' in result.message and result.match_ratio > 0.9
    assert calls == ['initialpose']


def test_moved_robot_is_found_by_rotating_global_search(graph, room):
    """The robot was moved while off: AMCL searches the map during a spin."""
    executor, select, client, calls = graph
    _saved(room, -0.9, -0.5, 2.0)
    select()
    result = _run(executor, client)
    assert result.success, result.message
    assert 'found by global search' in result.message
    assert calls[:3] == ['initialpose', 'global', 'spin'] and 'nomotion' in calls
    assert result.pose.pose.pose.position.x == pytest.approx(TRUE_POSE[0], abs=1e-3)
