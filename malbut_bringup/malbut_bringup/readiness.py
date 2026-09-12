"""Check startup inputs without sending goals or controlling the chassis."""

from functools import partial
import json
import math
import time

from lifecycle_msgs.msg import State
from lifecycle_msgs.srv import GetState
from malbut_interfaces.action import FollowPerson, Patrol
from nav2_msgs.action import BackUp, ComputePathToPose, FollowPath, NavigateToPose, Spin, Wait
from nav2_msgs.msg import Costmap
from nav_msgs.msg import OccupancyGrid, Odometry
import rclpy
from rclpy.action import ActionClient
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.time import Time
from sensor_msgs.msg import CameraInfo, Image, LaserScan
from std_msgs.msg import String
from tf2_ros import Buffer, TransformListener
from vision_msgs.msg import Detection3DArray


class RobotReadiness(Node):
    """Wait for actual data, transforms, and active Nav2 servers at startup."""

    def __init__(self):
        super().__init__('bringup_readiness')
        defaults = {
            'navigation': False, 'perception': True,
            'scan_topic': '/scan_raw', 'odom_topic': '/odom',
            'rgb_topic': '/depth_cam/rgb0/image_raw',
            'depth_topic': '/depth_cam/depth0/image_raw',
            'camera_info_topic': '/depth_cam/rgb0/camera_info',
            'global_frame': 'map', 'robot_frame': 'base_footprint',
            'static_map_topic': '/map',
            'global_costmap_topic': '/global_costmap/costmap_raw',
            'patrol_costmap_topic': '/global_costmap/costmap',
            'sensor_timeout_s': 3.0,
        }
        self.settings = {
            key: self.declare_parameter(key, default).value
            for key, default in defaults.items()
        }
        self.timeout = float(self.settings['sensor_timeout_s'])
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError('sensor_timeout_s must be positive and finite')
        self.ready = False
        self.seen = {}
        self.frames = {}
        self.fixed = set()
        self.subscriptions_ = []
        self.action_clients = []
        self.lifecycle = {}
        self.tf = Buffer()
        self.tf_listener = TransformListener(self.tf, self)
        sensor_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
        static_qos = QoSProfile(
            depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        self.status_publisher = self.create_publisher(
            String, '/malbut/bringup/status', static_qos)
        topics = [
            ('scan', LaserScan, self.settings['scan_topic']),
            ('odom', Odometry, self.settings['odom_topic']),
            ('rgb', Image, self.settings['rgb_topic']),
            ('depth', Image, self.settings['depth_topic']),
            ('camera_info', CameraInfo, self.settings['camera_info_topic']),
        ]
        if self.settings['perception']:
            topics.append(('perception', Detection3DArray,
                           '/perception/person/detections_3d'))
        for label, message_type, topic in topics:
            self.seen[label] = None
            self.subscriptions_.append(self.create_subscription(
                message_type, topic, partial(self._receive, label), sensor_qos))
        if self.settings['navigation']:
            for label, message_type, topic in (
                ('map', OccupancyGrid, self.settings['static_map_topic']),
                ('costmap', Costmap, self.settings['global_costmap_topic']),
                ('patrol_costmap', OccupancyGrid,
                 self.settings['patrol_costmap_topic']),
            ):
                self.fixed.add(label)
                self.seen[label] = None
                self.subscriptions_.append(self.create_subscription(
                    message_type, topic, partial(self._receive, label), static_qos))
            self.action_clients = [
                (name, ActionClient(self, action_type, name))
                for name, action_type in (
                    ('/navigate_to_pose', NavigateToPose),
                    ('/compute_path_to_pose', ComputePathToPose),
                    ('/follow_path', FollowPath), ('/spin', Spin),
                    ('/wait', Wait), ('/backup', BackUp),
                    ('/follow_person', FollowPerson), ('/patrol', Patrol),
                )
            ]
            self.lifecycle = {
                name: [self.create_client(GetState, f'/{name}/get_state'),
                       None, False]
                for name in ('amcl', 'map_server', 'controller_server',
                             'planner_server', 'behavior_server', 'bt_navigator')
            }
        self.last_missing = None
        self.timer = self.create_timer(1.0, self.check)

    def _receive(self, label, message):
        if not message.header.frame_id:
            return
        if label in ('rgb', 'depth') and (
                message.width == 0 or message.height == 0 or not message.data):
            return
        if label == 'camera_info' and (message.k[0] <= 0 or message.k[4] <= 0):
            return
        if label == 'scan' and not message.ranges:
            return
        if label in self.fixed and len(message.data) == 0:
            return
        # Header time must be wall time too; a recently received old image is
        # not evidence that the physical camera is delivering current data.
        if label not in self.fixed:
            age = (self.get_clock().now() - Time.from_msg(
                message.header.stamp)).nanoseconds * 1e-9
            if age < -self.timeout or age > self.timeout:
                return
        self.seen[label] = time.monotonic()
        self.frames[label] = message.header.frame_id

    def check(self):
        """Poll only readiness; no fixed boot sleep and no autonomous motion."""
        now = time.monotonic()
        missing = [
            f'data:{label}' for label, received in self.seen.items()
            if received is None or (
                label not in self.fixed and now - received > self.timeout)
        ]
        base = self.settings['robot_frame']
        for label in ('scan', 'camera_info', 'odom', 'rgb', 'depth', 'perception'):
            if label not in self.seen:
                continue
            frame = self.frames.get(label)
            if not frame or not self.tf.can_transform(base, frame, Time()):
                missing.append(f'TF:{label}->{base}')
        if self.settings['navigation']:
            target = self.settings['global_frame']
            if not self.tf.can_transform(target, base, Time()):
                missing.append(f'TF:{target}->{base} (set initial pose)')
            for label in self.fixed:
                if self.frames.get(label) != target:
                    missing.append(f'frame:{label} must be {target}')
        for name, client in self.action_clients:
            if not client.server_is_ready():
                missing.append(f'Action:{name}')
        for name, entry in self.lifecycle.items():
            client, future, active = entry
            if not client.service_is_ready():
                if future is not None:
                    future.cancel()
                entry[1] = None
                entry[2] = False
            elif future is None or future.done():
                if future is not None:
                    try:
                        entry[2] = future.result().current_state.id == State.PRIMARY_STATE_ACTIVE
                    except Exception:  # A disconnected lifecycle service is not ready.
                        entry[2] = False
                entry[1] = client.call_async(GetState.Request())
            if not entry[2]:
                missing.append(f'lifecycle:{name}')
        if missing:
            summary = ', '.join(missing)
            if summary != self.last_missing:
                self.get_logger().info('Waiting for ' + summary)
                self.status_publisher.publish(String(data=json.dumps({
                    'state': 'WAITING', 'missing': missing,
                })))
                self.last_missing = summary
            return
        self.ready = True
        self.status_publisher.publish(String(data=json.dumps({
            'state': 'READY', 'missing': [],
        })))
        self.get_logger().info('Required robot inputs are ready.')


def main(args=None):
    """Exit successfully only after all selected startup inputs are ready."""
    rclpy.init(args=args)
    node = None
    result = 1
    try:
        node = RobotReadiness()
        while rclpy.ok() and not node.ready:
            rclpy.spin_once(node, timeout_sec=1.0)
        if node.ready:
            result = 0
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return result
