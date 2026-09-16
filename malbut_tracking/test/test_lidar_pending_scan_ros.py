"""Exercise the C++ foreground node with delayed TF, without motion servers."""

import os
from pathlib import Path
import subprocess
from time import monotonic

from ament_index_python.packages import get_package_prefix
from geometry_msgs.msg import TransformStamped
from malbut_interfaces.msg import LidarClusterArray
from nav_msgs.msg import OccupancyGrid
import rclpy
from rclpy.context import Context
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data
from rclpy.time import Time
from rosgraph_msgs.msg import Clock
from sensor_msgs.msg import LaserScan
from tf2_ros import TransformBroadcaster


def test_delayed_tf_keeps_one_waiting_scan_and_only_the_latest_successor(tmp_path):
    """TF lag above the scan interval must not discard every waiting scan."""
    domain = 197
    context = Context()
    rclpy.init(context=context, domain_id=domain)
    node = Node('test_delayed_lidar_tf', context=context)
    executor = SingleThreadedExecutor(context=context)
    executor.add_node(node)
    clock_pub = node.create_publisher(Clock, '/clock', 10)
    map_pub = node.create_publisher(
        OccupancyGrid, '/pending_scan_test/map',
        QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL),
    )
    scan_pub = node.create_publisher(LaserScan, '/pending_scan_test/scan', 10)
    broadcaster = TransformBroadcaster(node)
    received = []
    subscription = node.create_subscription(
        LidarClusterArray, '/pending_scan_test/clusters',
        received.append, qos_profile_sensor_data,
    )
    executable = Path(get_package_prefix('malbut_tracking')) / (
        'lib/malbut_tracking/lidar_foreground_preprocessor'
    )
    log_path = tmp_path / 'preprocessor.log'
    process = None

    def spin_for(seconds):
        deadline = monotonic() + seconds
        while monotonic() < deadline:
            executor.spin_once(timeout_sec=0.01)

    def until(predicate):
        deadline = monotonic() + 5.0
        while not predicate():
            assert process.poll() is None, log_path.read_text()
            assert monotonic() < deadline, log_path.read_text()
            executor.spin_once(timeout_sec=0.01)

    def stamp(nanoseconds):
        return Time(nanoseconds=nanoseconds).to_msg()

    def clock(nanoseconds):
        clock_pub.publish(Clock(clock=stamp(nanoseconds)))
        spin_for(0.05)

    def scan(nanoseconds):
        message = LaserScan()
        message.header.frame_id = 'pending_scan_test_laser'
        message.header.stamp = stamp(nanoseconds)
        message.angle_min = -0.01
        message.angle_max = 0.01
        message.angle_increment = 0.01
        message.range_min = 0.1
        message.range_max = 5.0
        message.ranges = [1.0, 1.0, 1.0]
        scan_pub.publish(message)
        spin_for(0.05)

    def transform(nanoseconds):
        message = TransformStamped()
        message.header.frame_id = 'map'
        message.child_frame_id = 'pending_scan_test_laser'
        message.header.stamp = stamp(nanoseconds)
        message.transform.rotation.w = 1.0
        broadcaster.sendTransform(message)

    try:
        with log_path.open('w') as log:
            process = subprocess.Popen(
                [str(executable), '--ros-args', '-p', 'use_sim_time:=true',
                 '-p', 'scan_topic:=/pending_scan_test/scan',
                 '-p', 'static_map_topic:=/pending_scan_test/map',
                 '-p', 'clusters_topic:=/pending_scan_test/clusters'],
                env={**os.environ, 'ROS_DOMAIN_ID': str(domain)},
                stdout=log, stderr=subprocess.STDOUT,
            )
            until(lambda: scan_pub.get_subscription_count() > 0)
            until(lambda: clock_pub.get_subscription_count() > 0)
            until(lambda: broadcaster.pub_tf.get_subscription_count() > 0)
            grid = OccupancyGrid()
            grid.header.frame_id = 'map'
            grid.info.resolution = 0.1
            grid.info.width = grid.info.height = 100
            grid.info.origin.position.x = grid.info.origin.position.y = -5.0
            grid.info.origin.orientation.w = 1.0
            grid.data = [0] * 10000
            map_pub.publish(grid)
            until(lambda: 'Cached static distance field' in log_path.read_text())

            clock(10_000_000_000)
            scan(10_000_000_000)
            clock(10_040_000_000)
            scan(10_040_000_000)
            clock(10_080_000_000)
            scan(10_080_000_000)
            assert received == []
            # Only the first measurement has TF; replacing it on each input
            # would leave the node permanently waiting for a newer transform.
            transform(10_000_000_000)
            until(lambda: len(received) == 1)
            assert received[0].header.stamp == stamp(10_000_000_000)
            assert received[0].clusters[0].point_count == 3
            transform(10_080_000_000)
            until(lambda: len(received) == 2)
            assert received[1].header.stamp == stamp(10_080_000_000)
            spin_for(0.05)
            assert len(received) == 2  # The superseded middle scan was not queued.

            clock(20_000_000_000)
            scan(20_000_000_000)
            clock(20_050_000_000)
            scan(20_050_000_000)
            clock(20_310_000_000)  # Existing 0.30 s TF wait limit expires first.
            until(lambda: 'Dropping scan without' in log_path.read_text())
            transform(20_050_000_000)
            until(lambda: len(received) == 3)
            assert received[2].header.stamp == stamp(20_050_000_000)
            spin_for(0.05)
            assert len(received) == 3
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3.0)
        node.destroy_subscription(subscription)
        executor.shutdown()
        node.destroy_node()
        context.shutdown()
