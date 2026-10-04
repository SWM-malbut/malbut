"""Check startup inputs without sending goals or controlling the chassis."""

from collections import deque
from functools import partial
import json
import math
import time

from lifecycle_msgs.msg import State
from lifecycle_msgs.srv import GetState
from malbut_interfaces.action import AutoSlam, ExecuteMission, FollowPerson, Patrol, Relocalize
from nav2_msgs.action import (
    AssistedTeleop, BackUp, ComputePathToPose, FollowPath, NavigateToPose, Spin, Wait,
)
from nav2_msgs.msg import Costmap
from nav_msgs.msg import OccupancyGrid, Odometry
from rcl_interfaces.srv import GetParameters
import rclpy
from rclpy.action import ActionClient
from rclpy.duration import Duration
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

    def __init__(self, **kwargs):
        super().__init__('bringup_readiness', **kwargs)
        defaults = {
            'navigation': False, 'perception': True, 'relocalization': False,
            'scan_topic': '/scan_raw', 'odom_topic': '/odom',
            'rgb_topic': '/depth_cam/rgb0/image_raw',
            'depth_topic': '/depth_cam/depth0/image_raw',
            'camera_info_topic': '/depth_cam/rgb0/camera_info',
            'global_frame': 'map', 'robot_frame': 'base_footprint',
            'static_map_topic': '/map',
            'global_costmap_topic': '/global_costmap/costmap_raw',
            'patrol_costmap_topic': '/global_costmap/costmap',
            'sensor_timeout_s': 3.0,
            'startup_stage': '', 'startup_label': '', 'startup_index': 0,
            'startup_total': 0, 'startup_nodes': '', 'startup_timeout_s': 120.0,
            'observe_only': False, 'speech': False,
            'required_actions': '', 'required_topics': '',
        }
        self.settings = {
            key: self.declare_parameter(key, default).value
            for key, default in defaults.items()
        }
        self.timeout = float(self.settings['sensor_timeout_s'])
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError('sensor_timeout_s must be positive and finite')
        self.ready = False
        self.stage = self.settings['startup_stage']
        self.started_at = time.monotonic()
        self.startup_timeout = float(self.settings['startup_timeout_s'])
        if not math.isfinite(self.startup_timeout) or self.startup_timeout <= 0:
            raise ValueError('startup_timeout_s must be positive and finite')
        self.seen = {}
        self.frames = {}
        # Keep headers only: TF can arrive just after a scan, so testing only
        # the newest sample at each tick would reject a healthy stream.
        self.scan_headers = deque(maxlen=32)
        self.fixed = set()
        self.subscriptions_ = []
        self.action_clients = []
        self.lifecycle = {}
        self.lifecycle_requested = {}
        # The aggregate connection observer does not process sensor data or TF.
        self.tf = None if self.settings['observe_only'] else Buffer()
        self.tf_listener = (None if self.tf is None else TransformListener(self.tf, self))
        sensor_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
        static_qos = QoSProfile(
            depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        self.status_publisher = self.create_publisher(
            String, '/malbut/bringup/status', static_qos)
        self.progress_publisher = self.create_publisher(
            String, '/malbut/bringup/progress', static_qos) if self.stage else None
        self.probes = {
            name: [self.create_client(GetParameters, f'/{name}/get_parameters'), None, False]
            for name in self.settings['startup_nodes'].split(',') if name
        }
        self.probe_requested = {}
        self.speech_ready = False
        if self.stage == 'speech' or self.settings['speech']:
            self.subscriptions_.append(self.create_subscription(
                String, '/malbut/speech/status', self._speech, static_qos))
        topics = [
            ('scan', LaserScan, self.settings['scan_topic']),
            ('odom', Odometry, self.settings['odom_topic']),
            ('rgb', Image, self.settings['rgb_topic']),
            ('depth', Image, self.settings['depth_topic']),
            ('camera_info', CameraInfo, self.settings['camera_info_topic']),
        ]
        if self.stage in ('extensions', 'speech'):
            topics = []
        elif self.settings['perception']:
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
            actions = [
                ('/navigate_to_pose', NavigateToPose),
                ('/compute_path_to_pose', ComputePathToPose),
                ('/follow_path', FollowPath), ('/spin', Spin),
                ('/wait', Wait), ('/backup', BackUp),
                ('/patrol', Patrol), ('/assisted_teleop', AssistedTeleop),
                ('/autoslam', AutoSlam),
            ]
            if self.settings['perception']:
                actions.append(('/follow_person', FollowPerson))
            if self.settings['relocalization']:
                actions.append(('/relocalize', Relocalize))
            if self.stage == 'navigation':
                # These applications start only after the navigation group.
                actions = [(name, kind) for name, kind in actions
                           if name not in ('/patrol', '/autoslam', '/follow_person')]
            self.action_clients = [
                (name, ActionClient(self, action_type, name))
                for name, action_type in actions
            ]
            # SLAM or AMCL is selected by the system manager, so readiness
            # checks the resulting map and TF rather than AMCL's lifecycle.
            # Navigation reaches /cmd_vel through the smoother, manual driving
            # through its AssistedTeleop server and the Collision Monitor.
            # entry: [client, pending GetState future, active, last known label]
            self.lifecycle = {
                name: [self.create_client(GetState, f'/{name}/get_state'),
                       None, False, 'missing']
                for name in ('controller_server', 'planner_server',
                             'behavior_server', 'teleop_behavior_server', 'bt_navigator',
                             'velocity_smoother', 'collision_monitor')
            }
        if self.stage in ('following', 'patrol'):
            name, action_type = (
                ('/follow_person', FollowPerson) if self.stage == 'following'
                else ('/patrol', Patrol))
            self.action_clients = [(name, ActionClient(self, action_type, name))]
        if self.settings['observe_only']:
            self.lifecycle = {
                name: [self.create_client(GetState, f'/{name}/get_state'),
                       None, False, 'missing']
                for name in ('controller_server', 'planner_server', 'behavior_server',
                             'teleop_behavior_server', 'bt_navigator',
                             'velocity_smoother', 'collision_monitor')
            }
            kinds = {'/malbut/mission/execute': ExecuteMission, '/autoslam': AutoSlam,
                     '/follow_person': FollowPerson, '/patrol': Patrol,
                     '/relocalize': Relocalize, '/navigate_to_pose': NavigateToPose,
                     '/compute_path_to_pose': ComputePathToPose, '/follow_path': FollowPath,
                     '/spin': Spin, '/wait': Wait, '/backup': BackUp,
                     '/assisted_teleop': AssistedTeleop}
            self.action_clients = [(name, ActionClient(self, kinds[name], name))
                                   for name in self.settings['required_actions'].split(',')
                                   if name]
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
        if label not in self.fixed or label in ('costmap', 'patrol_costmap'):
            age = (self.get_clock().now() - Time.from_msg(
                message.header.stamp)).nanoseconds * 1e-9
            if age < -self.timeout or age > self.timeout:
                return
        self.seen[label] = time.monotonic()
        self.frames[label] = message.header.frame_id
        if label == 'scan':
            stamp = Time.from_msg(message.header.stamp)
            if stamp.nanoseconds > 0:
                self.scan_headers.append((self.seen[label], message.header.frame_id, stamp))

    def _scan_transform_ready(self, target, now):
        """Require repeated recent scans usable at their acquisition times."""
        if not target:
            return False
        clock_now = self.get_clock().now()
        matched_stamps = set()
        for received, frame, stamp in reversed(self.scan_headers):
            if (now - received > self.timeout
                    or frame != self.frames.get('scan')
                    or not 0 <= (clock_now - stamp).nanoseconds * 1e-9 <= self.timeout):
                continue
            if self.tf.can_transform(target, frame, stamp):
                matched_stamps.add(stamp.nanoseconds)
                if len(matched_stamps) >= 2:
                    return True
        return False

    def check(self):
        """Poll only readiness; no fixed boot sleep and no autonomous motion."""
        if self.settings.get('observe_only'):
            missing = self._extension_missing()
            missing.extend(self._lifecycle_missing(time.monotonic()))
            missing.extend(f'Action:{name}' for name, client in self.action_clients
                           if not client.server_is_ready())
            missing.extend(f'publisher:{topic}'
                           for topic in self.settings['required_topics'].split(',')
                           if topic and not self.count_publishers(topic))
            self._report(missing)
            return
        if getattr(self, 'stage', '') in ('extensions', 'speech'):
            self._report(self._extension_missing())
            return
        now = time.monotonic()
        missing = [
            f'data:{label}' for label, received in self.seen.items()
            if received is None or (
                (label not in self.fixed or label in ('costmap', 'patrol_costmap'))
                and now - received > self.timeout)
        ]
        base = self.settings['robot_frame']
        for label in ('scan', 'camera_info', 'odom', 'rgb', 'depth', 'perception'):
            if label not in self.seen:
                continue
            frame = self.frames.get(label)
            if not frame or not self.tf.can_transform(base, frame, Time()):
                missing.append(f'TF:{label}->{base}')
        # A static base->lidar transform says nothing about whether AMCL and
        # costmaps can transform the actual scans through the dynamic odom TF.
        odom = self.frames.get('odom')
        if not self._scan_transform_ready(odom, now):
            missing.append(f'TF:scan->{odom or "odom"}@stamp')
        if self.settings['navigation']:
            target = self.settings['global_frame']
            if not self.tf.can_transform(target, base, Time()):
                missing.append(f'TF:{target}->{base} (set initial pose)')
            if not self._scan_transform_ready(target, now):
                missing.append(f'TF:scan->{target}@stamp')
            for label in self.fixed:
                if label in self.frames and self.frames[label] != target:
                    missing.append(f'frame:{label} must be {target}')
        for name, client in self.action_clients:
            if not client.server_is_ready():
                missing.append(f'Action:{name}')
        missing.extend(self._lifecycle_missing(now))
        self._report(missing)

    def _lifecycle_missing(self, now):
        missing = []
        for name, entry in self.lifecycle.items():
            client, future, active = entry[:3]
            if (future is not None and not future.done()
                    and now - self.lifecycle_requested.get(name, now) >= self.timeout):
                # A lost GetState response must not block startup forever.
                # Reuse the existing readiness timeout; this is a read-only query.
                client.remove_pending_request(future)
                future.cancel()
                future = entry[1] = None
                entry[2] = False
                entry[3] = 'response_timeout'
            if not client.service_is_ready():
                if future is not None:
                    client.remove_pending_request(future)
                    future.cancel()
                entry[1] = None
                entry[2] = False
                # Discovery absence does not prove the process exited or that
                # the component never loaded; DDS/executor failure can look alike.
                entry[3] = 'service_not_discovered'
            elif future is None or future.done():
                if future is not None:
                    try:
                        state = future.result().current_state
                        entry[2] = state.id == State.PRIMARY_STATE_ACTIVE
                        entry[3] = state.label
                    except Exception:  # A disconnected lifecycle service is not ready.
                        entry[2] = False
                        entry[3] = 'no reply'
                entry[1] = client.call_async(GetState.Request())
                self.lifecycle_requested[name] = now
            if not entry[2]:
                # The Nav2 lifecycle manager configures nodes in order and waits
                # forever for one whose services never appear; the labels show
                # where it stopped (e.g. inactive up to the missing node).
                missing.append(f'lifecycle:{name}={entry[3]}')
        return missing

    def _speech(self, message):
        self.speech_ready = message.data == 'ready'

    def _extension_missing(self):
        # A service response proves the executor is spinning after constructor
        # initialization, unlike merely discovering a process/node name. Never
        # enable monitoring or require a person/cloud permission for startup.
        now = time.monotonic()
        missing = []
        for name, entry in self.probes.items():
            client, future, ready = entry
            observe = self.settings.get('observe_only')
            if observe and not client.service_is_ready():
                if future is not None:
                    client.remove_pending_request(future)
                    future.cancel()
                entry[1], entry[2] = None, False
                missing.append(f'init:{name}')
                continue
            if ready and not self.settings.get('observe_only'):
                continue
            if future is not None and future.done():
                try:
                    entry[2] = future.result() is not None
                except Exception:
                    entry[2] = False
                entry[1] = None
            elif future is not None and now - self.probe_requested[name] >= self.timeout:
                client.remove_pending_request(future)
                future.cancel()
                entry[1] = None
                entry[2] = False
            if not entry[2]:
                missing.append(f'init:{name}')
            if (observe or not entry[2]) and entry[1] is None and client.service_is_ready():
                entry[1] = client.call_async(GetParameters.Request(names=['use_sim_time']))
                self.probe_requested[name] = now
        if (self.stage == 'speech' or self.settings.get('speech')) and not self.speech_ready:
            missing.append('speech: microphone startup')
        return missing

    def _report(self, missing):
        if self.settings.get('observe_only'):
            total = (len(self.probes) + len(self.action_clients) + len(self.lifecycle)
                     + sum(bool(topic) for topic in self.settings['required_topics'].split(','))
                     + int(self.settings['speech']))
            state = 'WAITING' if missing else 'READY'
            self.ready = not missing
            payload = {'state': state, 'missing': missing}
            self.status_publisher.publish(String(data=json.dumps(payload)))
            self.progress_publisher.publish(String(data=json.dumps({
                **payload, 'completed': max(0, total - len(missing)), 'total': total,
                'stage': '노드·인터페이스 연결 확인',
            })))
            return  # Informational only: no timeout, shutdown or mission gate.
        stage = getattr(self, 'stage', '')
        if stage:
            expired = bool(missing) and time.monotonic() - self.started_at >= self.startup_timeout
            index, total = self.settings['startup_index'], self.settings['startup_total']
            self.progress_publisher.publish(String(data=json.dumps({
                'completed': index - int(bool(missing)), 'total': total,
                'stage': self.settings['startup_label'], 'missing': missing,
                'state': 'ERROR' if expired else 'WAITING' if missing else 'READY',
            })))
            if expired:
                raise RuntimeError(
                    f"Startup timed out: {self.settings['startup_label']}: {', '.join(missing)}")
        if missing:
            summary = ', '.join(missing)
            if summary != self.last_missing:
                self.get_logger().info('Waiting for ' + summary)
                if stage in ('', 'applications'):
                    self.status_publisher.publish(String(data=json.dumps({
                        'state': 'WAITING', 'missing': missing,
                    })))
                self.last_missing = summary
            return
        self.ready = True
        if stage in ('', 'applications'):
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
        while rclpy.ok() and (node.settings['observe_only'] or not node.ready):
            rclpy.spin_once(node, timeout_sec=1.0)
        if node.ready:
            # Deliver the final count before this one-shot publisher exits.
            if node.progress_publisher is not None:
                node.progress_publisher.wait_for_all_acked(Duration(seconds=1.0))
            node.status_publisher.wait_for_all_acked(Duration(seconds=1.0))
            result = 0
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return result
