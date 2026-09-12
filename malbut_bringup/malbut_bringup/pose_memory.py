"""Restore once, then periodically save fresh AMCL estimates for a saved map."""

import math
from pathlib import Path
import time

from geometry_msgs.msg import PoseWithCovarianceStamped
from lifecycle_msgs.msg import State
from lifecycle_msgs.srv import GetState
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

from .pose_store import map_identity, read_pose, valid_pose, write_pose


class PoseMemory(Node):
    """Keep localization memory independent of missions and navigation goals."""

    def __init__(self):
        super().__init__('pose_memory')
        self.declare_parameter('map', '')
        self.declare_parameter('pose_file', str(
            Path.home() / '.ros/malbut/localization/last_pose.yaml'))
        self.declare_parameter('save_period_s', 5.0)
        self.declare_parameter('restore_pose', True)
        self.period = float(self.get_parameter('save_period_s').value)
        if not math.isfinite(self.period) or self.period <= 0:
            raise ValueError('save_period_s must be positive')
        self.path = self.get_parameter('pose_file').value
        self.identity = map_identity(self.get_parameter('map').value)
        self.saved = read_pose(self.path, self.identity)
        self.restore_done = not (self.saved and self.get_parameter('restore_pose').value)
        self.latest = None
        self.received = 0.0
        self.saved_received = 0.0
        self.initial_stamp = 0
        self.future = None
        self.active = False
        self.publisher = self.create_publisher(
            PoseWithCovarianceStamped, '/initialpose', 10)
        # Sensor-compatible reliability and VOLATILE avoid restoring an old,
        # latched estimate in preference to the persisted/map-checked record.
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT,
                         durability=DurabilityPolicy.VOLATILE)
        self.create_subscription(PoseWithCovarianceStamped, '/amcl_pose', self._receive, qos)
        self.create_subscription(
            PoseWithCovarianceStamped, '/initialpose', self._initialpose, 10)
        self.client = self.create_client(GetState, '/amcl/get_state')
        self.create_timer(1.0, self._restore)
        self.create_timer(self.period, self.save_latest)
        if self.saved is None:
            self.get_logger().info('No pose for this map. Set initial pose in RViz.')

    def _initialpose(self, message):
        if message.header.frame_id == 'map':
            # A manual localization request always wins over startup memory.
            self.restore_done = True
            self.latest = None
            self.initial_stamp = self.get_clock().now().nanoseconds

    def _receive(self, message):
        if message.header.frame_id != 'map':
            return
        now = self.get_clock().now().nanoseconds
        stamp = message.header.stamp.sec * 10**9 + message.header.stamp.nanosec
        if (stamp <= 0 or stamp < self.initial_stamp
                or not 0 <= (now - stamp) / 1e9 <= 2 * self.period):
            return
        position = message.pose.pose.position
        q = message.pose.pose.orientation
        norm = math.sqrt(q.x*q.x + q.y*q.y + q.z*q.z + q.w*q.w)
        if not math.isfinite(norm) or norm == 0:
            return
        x, y, z, w = q.x / norm, q.y / norm, q.z / norm, q.w / norm
        pose = {'frame_id': 'map', 'x': position.x, 'y': position.y,
                'yaw': math.atan2(2*(w*z + x*y), 1 - 2*(y*y + z*z)),
                'covariance': [float(value) for value in message.pose.covariance]}
        if valid_pose(pose):
            self.latest = pose
            self.received = time.monotonic()
            if self.active:
                self.restore_done = True  # Don't overwrite an existing estimate.

    def _restore(self):
        if not self.client.service_is_ready():
            self.active = False
            if self.future is not None:
                # A request to the previous server may never receive a reply.
                self.client.remove_pending_request(self.future)
                self.future.cancel()
                self.future = None
            return
        if self.future is None:
            self.future = self.client.call_async(GetState.Request())
            return
        if not self.future.done():
            return
        try:
            self.active = self.future.result().current_state.id == State.PRIMARY_STATE_ACTIVE
        except Exception:
            self.active = False
        self.future = None
        if (self.active and self.latest is not None
                and time.monotonic() - self.received <= 2 * self.period):
            # A stationary AMCL may publish just once before GetState replies.
            self.restore_done = True
        amcl_listening = any(info.node_name == 'amcl' for info in
                             self.get_subscriptions_info_by_topic('/initialpose'))
        if self.restore_done or not self.active or not amcl_listening:
            return
        msg = PoseWithCovarianceStamped()
        msg.header.frame_id = 'map'
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.pose.pose.position.x = float(self.saved['x'])
        msg.pose.pose.position.y = float(self.saved['y'])
        msg.pose.pose.orientation.z = math.sin(self.saved['yaw'] / 2)
        msg.pose.pose.orientation.w = math.cos(self.saved['yaw'] / 2)
        msg.pose.covariance = [float(x) for x in self.saved['covariance']]
        self.restore_done = True
        self.publisher.publish(msg)
        self.get_logger().info(
            'Restored last AMCL pose as an initial estimate only. '
            'If the robot was moved while off, correct it in RViz before driving.')

    def save_latest(self):
        """Write only a new, recent estimate while AMCL is active."""
        if (not self.active or self.latest is None or self.received == self.saved_received
                or time.monotonic() - self.received > 2 * self.period):
            return
        try:
            write_pose(self.path, self.identity, self.latest)
            self.saved_received = self.received
        except OSError as error:
            self.get_logger().error(f'Cannot save localization pose: {error}')


def main(args=None):
    """Run passive pose memory; never request navigation or publish velocity."""
    rclpy.init(args=args)
    node = None
    try:
        node = PoseMemory()
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        if node is not None:
            node.save_latest()
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
